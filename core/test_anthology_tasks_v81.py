from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.anthology_tasks import TASK_CHOICES
from core.edit_versions import version_of
from core.views.supervision import _task_forms
from illustrations.models import Illustrator
from people.models import Person
from texts.models import Anthology, AnthologyTask, Text
from workflow.tests import create_member


class AnthologyTaskPanelTests(TestCase):
    def setUp(self):
        self.manager = create_member('tasks-manager', 'Koordynator redakcji')
        self.member = create_member('tasks-member', 'Redaktor')
        self.book = Anthology.objects.create(title='Antologia testowa')
        self.url = reverse('core:anthology_detail', args=[self.book.pk])
        self.client.force_login(self.manager)

    def token(self):
        return signing.dumps(
            [self.manager.pk, f'texts.anthology:{self.book.pk}', version_of(self.book)],
            salt='cms-edit-version',
        )

    def new_person(self, kind='blurb', **values):
        return self.client.post(self.url, {
            'action': 'new_person', '_edit_version': self.token(),
            'new-person-first_name': 'Nowa', 'new-person-last_name': kind,
            'new-person-task_type': kind, **values,
        })

    def all_tasks_data(self):
        return {
            'action': 'tasks_and_cover', '_edit_version': self.token(),
            'cover-cover_status': 'not_started',
            **{f'{kind}-status': 'not_commissioned' for kind, _ in TASK_CHOICES if kind != 'cover'},
        }

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

    def test_all_five_tasks_create_external_contact_without_account_or_roles(self):
        users_before = get_user_model().objects.count()
        for kind, _ in TASK_CHOICES:
            if kind == 'cover':
                continue
            with self.subTest(kind=kind):
                response = self.new_person(kind)
                self.assertEqual(response.status_code, 302)
                task = self.book.production_tasks.get(task_type=kind)
                person = task.assigned_to
                self.assertEqual(task.status, 'commissioned')
                self.assertIsNotNone(task.commissioned_at)
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
