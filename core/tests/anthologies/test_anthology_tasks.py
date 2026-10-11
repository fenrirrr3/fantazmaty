"""Zadania antologii: panel sześciu zadań, nowe osoby do zadań, zbiorczy zapis, zdejmowanie przypisań, „Nie dotyczy” i lista zadań."""
from datetime import date
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.anthology_tasks import TASK_CHOICES
from core.models import AudioContributor, Recruitment
from core.views.supervision import _task_forms
from illustrations.models import Illustrator
from people.models import Person
from texts.models import Anthology, AnthologyTask, ForeignAuthor, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage

from .support import AnthologyPageData


class AnthologyTaskPanelTests(AnthologyPageData, TestCase):
    def add_new(self, kind, name):
        return self.new_person(kind, **{'new-person-first_name': name, 'new-person-last_name': 'Nowa osoba'})

    def test_six_panels_and_separate_contact_form_with_grouped_linked_issues(self):
        texts = [Text.objects.create(title=title, anthology=self.book, length=100)
                 for title in ('Pierwszy tekst', 'Drugi <tekst>')]
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        document = html.fromstring(response.content)
        panels = document.xpath('//div[contains(@class,"anthology-six-tasks")]/fieldset')
        self.assertEqual([p.get('id') for p in panels], [f'task-{k}' for k, _ in TASK_CHOICES])
        self.assertEqual(len(document.xpath('//input[@name="new-person-first_name"]')), 1)
        self.assertFalse(document.xpath('//input[@name="cover-new_cover_first_name"]'))
        self.assertFalse(document.xpath('//form//form'))
        grouped = document.xpath('//ul[@class="anthology-checklist"]/li[strong[contains(.,"Tekst nie jest gotowy")]]')
        self.assertEqual(len(grouped), 1)
        self.assertEqual(set(grouped[0].xpath('./a/text()')), {t.title for t in texts})
        self.assertEqual(set(grouped[0].xpath('./a/@href')),
                         {reverse('core:assigned_text_detail', args=[t.pk]) for t in texts})
        self.assertContains(response, 'Drugi &lt;tekst&gt;')

    def test_each_task_can_be_cleared_then_assigned_to_new_external_contact(self):
        users_before = get_user_model().objects.count()
        for kind in AnthologyTask.TaskType.values:
            with self.subTest(kind=kind):
                task = self.book.production_tasks.get(task_type=kind)
                task.assigned_to = self.member.person_profile
                task.status = 'commissioned'
                task.save()
                self.assertIsNotNone(task.commissioned_at)
                document = html.fromstring(self.client.get(self.url).content)
                self.assertTrue(document.xpath(
                    f'//fieldset[@id="task-{kind}"]//button[@name="remove_task"][@formnovalidate]'))
                self.assertEqual(self.remove(kind).status_code, 302)
                task.refresh_from_db()
                self.assertEqual(task.status, 'not_commissioned')
                self.assertIsNone(task.assigned_to_id)
                self.assertIsNone(task.commissioned_at)
                self.assertTrue(Person.objects.filter(pk=self.member.person_profile.pk).exists())
                self.assertEqual(self.add_new(kind, kind).status_code, 302)
                task.refresh_from_db()
                person = task.assigned_to
                self.assertEqual(task.status, 'commissioned')
                self.assertIsNotNone(task.commissioned_at)
                self.assertEqual(person.first_name, kind)
                self.assertTrue(person.is_external)
                self.assertFalse(person.is_active)
                self.assertIsNone(person.user_id)
                self.assertIsNone(person.email)
                self.assertFalse(person.roles.exists())
        self.assertEqual(get_user_model().objects.count(), users_before)

    def test_cover_creates_inactive_illustrator_and_commissions_cover(self):
        people_before = Person.objects.count()
        self.assertEqual(self.new_person('cover').status_code, 302)
        self.book.refresh_from_db()
        artist = self.book.cover_illustrator
        self.assertIsNone(artist.email)
        self.assertFalse(artist.is_active)
        self.assertTrue(artist.covers)
        self.assertEqual(self.book.cover_status, 'in_progress')
        self.assertIsNotNone(self.book.cover_commissioned_at)
        self.assertEqual(Person.objects.count(), people_before)

    def test_duplicate_existing_inactive_contact_rejected_and_available_in_task_choice(self):
        person = Person.objects.create(first_name='Nowa', last_name='blurb', is_external=True)
        count = Person.objects.count()
        response = self.new_person(**{'new-person-first_name': ' nowa ', 'new-person-last_name': ' BLURB '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Person.objects.count(), count)
        self.assertFalse(self.book.production_tasks.exclude(status='not_commissioned', assigned_to=None).exists())
        field = self.client.get(self.url).context['task_forms'][1].fields['assigned_to']
        # Inactive and external people are offered only once they have worked on a task.
        self.assertFalse(field.queryset.filter(pk=person.pk).exists())
        earlier = Anthology.objects.create(title='Wcześniejsza praca').production_tasks.get(task_type='banners')
        earlier.assigned_to, earlier.status = person, 'commissioned'
        earlier.save()
        field = self.client.get(self.url).context['task_forms'][1].fields['assigned_to']
        self.assertTrue(field.queryset.filter(pk=person.pk).exists())
        self.assertTrue(_task_forms(self.book)[1].fields['assigned_to'].queryset.filter(pk=person.pk).exists())
        data = self.all_tasks_data()
        data.update({'blurb-status': 'commissioned', 'blurb-assigned_to': person.pk})
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.book.production_tasks.get(task_type='blurb').assigned_to, person)

    def test_duplicate_illustrator_is_not_created(self):
        Illustrator.objects.create(first_name='Nowa', last_name='cover', is_active=False)
        self.assertEqual(self.new_person('cover').status_code, 400)
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_occupied_task_and_cover_are_not_overwritten_or_given_orphan_contacts(self):
        AnthologyTask.objects.update_or_create(anthology=self.book, task_type='blurb',
                                               defaults={'status': 'ready', 'assigned_to': self.member.person_profile})
        count = Person.objects.count()
        self.assertEqual(self.new_person().status_code, 400)
        self.assertEqual(Person.objects.count(), count)
        self.assertEqual(self.book.production_tasks.get(task_type='blurb').status, 'ready')
        self.book.cover_author = 'Historyczna osoba'
        self.book.cover_status = 'ready'
        self.book.save()
        self.assertEqual(self.new_person('cover').status_code, 400)
        self.assertFalse(Illustrator.objects.exists())

    def test_invalid_contact_and_missing_or_stale_version_do_not_save(self):
        before = Person.objects.count()
        self.assertEqual(self.new_person(**{'new-person-last_name': ''}).status_code, 400)
        self.assertEqual(self.new_person(**{'_edit_version': ''}).status_code, 409)
        old_token = self.token()
        self.assertEqual(self.new_person().status_code, 302)
        self.assertEqual(self.new_person('banners', **{'_edit_version': old_token}).status_code, 409)
        self.assertEqual(Person.objects.count(), before + 1)
        task = self.book.production_tasks.get(task_type='banners')
        self.assertEqual(task.status, 'not_commissioned')
        self.assertIsNone(task.assigned_to_id)

    def test_member_cannot_add_contact_or_edit_tasks(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.url), 'Dodaj osobę do zadania')
        before = Person.objects.count()
        self.assertEqual(self.new_person().status_code, 403)
        self.assertEqual(self.client.post(self.url, self.all_tasks_data()).status_code, 403)
        self.assertEqual(Person.objects.count(), before)

    def test_combined_save_validates_every_task_and_cover_before_writing(self):
        data = self.all_tasks_data()
        data.update({'blurb-status': 'commissioned', 'blurb-assigned_to': self.member.person_profile.pk,
                     'cover-cover_status': 'ready'})
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.assertFalse(self.book.production_tasks.exclude(status='not_commissioned', assigned_to=None).exists())
        artist = Illustrator.objects.create(first_name='Jan', last_name='Rysuje')
        data.update({'cover-cover_illustrator': artist.pk, 'typesetting-status': 'ready'})
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.cover_illustrator_id)
        self.assertFalse(self.book.production_tasks.exclude(status='not_commissioned', assigned_to=None).exists())
        data['typesetting-assigned_to'] = self.member.person_profile.pk
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_illustrator, artist)
        self.assertEqual(self.book.cover_status, 'ready')
        self.assertEqual(self.book.production_tasks.count(), 5)
        self.assertEqual(self.book.production_tasks.get(task_type='typesetting').status, 'ready')

    def test_legacy_and_linked_cover_clear_completely_keep_notes_and_allow_new_contact(self):
        artist = Illustrator.objects.create(first_name='Dotychczasowy', last_name='Ilustrator')
        for contact in (None, artist):
            with self.subTest(contact=contact):
                self.book.refresh_from_db()
                self.book.cover_author = 'Historyczny podpis'
                self.book.cover_illustrator = contact
                self.book.cover_status = 'ready'
                self.book.cover_notes = 'Zachowaj uwagi'
                self.book.save()
                # A finished cover keeps its credit; removal is offered only before completion.
                self.assertEqual(self.remove('cover').status_code, 302)
                self.book.refresh_from_db()
                self.assertEqual(self.book.cover_status, 'ready')
                self.book.cover_status = 'in_progress'
                self.book.save()
                document = html.fromstring(self.client.get(self.url).content)
                self.assertTrue(document.xpath(
                    '//fieldset[@id="task-cover"]//button[@name="remove_task"][@formnovalidate]'))
                self.assertEqual(self.remove('cover').status_code, 302)
                self.book.refresh_from_db()
                self.assertIsNone(self.book.cover_illustrator_id)
                self.assertEqual(self.book.cover_author, '')
                self.assertEqual(self.book.cover_status, 'not_started')
                self.assertIsNone(self.book.cover_commissioned_at)
                self.assertEqual(self.book.cover_notes, 'Zachowaj uwagi')
                self.assertTrue(Illustrator.objects.filter(pk=artist.pk).exists())
                self.assertNotContains(self.client.get(self.url), 'Dotychczasowy zapis:')
                self.assertEqual(self.add_new('cover', f'Nowy{contact is None}').status_code, 302)
                self.book.refresh_from_db()
                self.assertEqual(self.book.cover_status, 'in_progress')
                self.assertTrue(self.book.cover_illustrator_id)

    def test_removal_is_independent_of_unsaved_other_fields(self):
        task = self.book.production_tasks.get(task_type='blurb')
        task.assigned_to = self.member.person_profile
        task.status = 'commissioned'
        task.save()
        self.book.cover_author = 'Stara okładka'
        self.book.cover_status = 'ready'
        self.book.save()
        self.assertEqual(self.remove('blurb', **{
            'cover-cover_status': 'not_started', 'cover-clear_legacy_cover': 'on',
            'banners-status': 'invalid', 'blurb-assigned_to': 'invalid',
        }).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, 'Stara okładka')
        self.assertEqual(self.book.cover_status, 'ready')
        task.refresh_from_db()
        self.assertEqual(task.status, 'not_commissioned')

    def test_removal_requires_permission_current_version_and_known_task(self):
        self.book.cover_author = 'Nie usuwaj'
        self.book.cover_status = 'ready'
        self.book.save()
        old = self.token()
        self.book.cover_notes = 'Zmiana'
        self.book.save()
        self.assertEqual(self.remove('cover', _edit_version=old).status_code, 409)
        self.assertEqual(self.remove('cover', _edit_version='').status_code, 409)
        self.assertEqual(self.remove('unknown').status_code, 400)
        self.client.force_login(self.member)
        self.assertEqual(self.remove('cover').status_code, 403)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, 'Nie usuwaj')


class AnthologyTaskStatusTests(AnthologyPageData, TestCase):
    def test_not_applicable_is_done_without_an_assignee(self):
        task = self.book.production_tasks.get(task_type='banners')
        task.assigned_to = self.member.person_profile
        task.status = AnthologyTask.Status.COMMISSIONED
        task.save()
        task.status = AnthologyTask.Status.NOT_APPLICABLE
        task.full_clean()
        task.save()
        task.refresh_from_db()
        self.assertIsNone(task.assigned_to_id)
        self.assertIsNone(task.commissioned_at)
        self.assertIn(task.status, AnthologyTask.DONE_STATUSES)
        self.assertEqual(task.get_status_display(), 'Nie dotyczy')

    def test_not_applicable_audio_description_keeps_its_stage(self):
        task = self.book.production_tasks.get(task_type='audio_description')
        task.status = AnthologyTask.Status.NOT_APPLICABLE
        task.save()
        description = self.book.audio_description
        description.content = 'Szkic'
        description.save()
        task.refresh_from_db()
        self.assertEqual(task.status, AnthologyTask.Status.NOT_APPLICABLE)

    def test_task_list_one_row_six_status_columns_cover_dates_and_coordinator_access(self):
        self.book.cover_author = 'Ilustrator'
        self.book.cover_status = 'in_progress'
        with patch('texts.models.timezone.localdate', return_value=date(2026, 9, 1)):
            self.book.save(update_fields=['cover_status', 'cover_author'])
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_commissioned_at, date(2026, 9, 1))
        self.book.cover_status = 'ready'
        self.book.save(update_fields=['cover_status'])
        self.assertEqual(self.book.cover_commissioned_at, date(2026, 9, 1))
        page = self.client.get(reverse('core:task_list'))
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        tree = html.fromstring(page.content)
        self.assertEqual(len(tree.xpath('//table/thead/tr/th')), 7)
        self.assertContains(page, '01.09.2026')
        self.assertContains(page, 'Typografia okładki')
        for key in ['cover', *AnthologyTask.TaskType.values]:
            self.assertEqual(self.client.get(reverse('core:task_list'), {'sort': key}).status_code, 200)
        self.book.cover_status = 'not_started'
        self.book.cover_author = ''
        self.book.save(update_fields=['cover_status', 'cover_author'])
        self.assertIsNone(self.book.cover_commissioned_at)
        url = reverse('core:task_list')
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotContains(self.client.get(reverse('core:tag_list')), 'href="' + url + '"')

    def test_fixed_tasks_cannot_be_added_or_deleted_in_admin(self):
        request = RequestFactory().get('/')
        request.user = self.admin
        model_admin = admin.site.get_model_admin(AnthologyTask)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))


class TaskNavigationTests(AnthologyPageData, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Antologia tłumaczona w toku pracy oraz kontakty spoza procesu tekstów, jak w danych produkcyjnych.
        cls.translated = Anthology.objects.create(title='Przekłady nawigacyjne', is_translated=True)
        text = Text.objects.create(title='Podróż nawigacyjna', anthology=cls.translated, length=100)
        text.translation.foreign_authors.add(
            ForeignAuthor.objects.create(first_name='Secret', last_name='Name', pseudonym='Signature'))
        assignment = WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=cls.member)
        WorkflowStage.objects.create(text=text, stage_type='editing', assignment=assignment,
                                     started_at=timezone.localdate())
        AudioContributor.objects.create(name='Nawigator lektor', email='secret-audio@example.test')
        Illustrator.objects.create(first_name='Nawigator', last_name='Plastyk', is_active=False)
        Recruitment.objects.create(first_name='Nawigator', last_name='Kandydat', email='secret-candidate@example.test')

    def test_task_links_reach_visible_novel_form_and_ad_forms_warn_separately(self):
        novel = Anthology.objects.create(title='Zadania powieści', is_novel=True)
        response = self.client.get(reverse('core:novel_detail', args=[novel.pk]), {'tasks': '1'})
        doc = html.fromstring(response.content)
        self.assertTrue(doc.xpath('//details[@id="novel-production"][@open]'))
        self.assertTrue(doc.xpath('//*[@id="task-blurb"]'))
        response = self.client.get(reverse('core:audio_description_detail', args=[self.translated.pk]))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//form[@data-warn-unsaved]')), 4)
        self.assertTrue(doc.xpath('//form[@data-autosave]/input[@name="action"][@value="content"]'))
