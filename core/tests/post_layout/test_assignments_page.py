"""Strona Korekta poskładowa: dostęp według ról, dodawanie przydziałów, lista, filtry i profil osoby."""
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.models import PostLayoutAssignment
from core.post_layout import AssignmentForm, create_assignment
from core.selectors.people import profile_assignments
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.availability import claim_access, eligible_role_users, role_access_reason
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.read_queries import available_stages
from workflow.services import claim_stage

from .support import PostLayoutTestData


class PostLayoutAccessTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('layout-admin', 'admin@example.test', 'test')
        self.member = get_user_model().objects.create_user('layout-member')
        self.person = Person.objects.create(user=self.member, first_name='Jan', last_name='Test')
        self.book = Anthology.objects.create(title='Antologia')
        self.text = Text.objects.create(title='Tekst', anthology=self.book, length=100)

    def roles(self, *names):
        self.person.roles.set([Role.objects.get_or_create(name=name)[0] for name in names])

    def test_access_by_role_and_post_layout_role_does_not_grant_text_proofreading(self):
        self.client.force_login(self.member)
        route = reverse('core:post_layout')
        for roles, allowed in [((), False), (('Korektor',), False), (('Koordynator redakcji',), True),
                               (('Koordynator',), True), (('Korektor poskładowy',), True),
                               (('Koordynator korekty',), True)]:
            self.roles(*roles)
            self.assertEqual(self.client.get(route).status_code, 200 if allowed else 403)
            doc = html.fromstring(self.client.get(reverse('core:home')).content)
            self.assertEqual(bool(doc.xpath(f'//nav//a[@href="{route}"]')), allowed)
        # Korektor poskładowy nie jest korektorem tekstów w procesie redakcyjnym.
        self.roles('Korektor poskładowy')
        stage = WorkflowStage.objects.create(text=self.text, stage_type='first_proofreading')
        role = WorkflowRoleAssignment.Role.PROOFREADER_1
        self.assertTrue(role_access_reason(self.member, role))
        self.assertFalse(eligible_role_users(role).filter(pk=self.member.pk).exists())
        self.assertFalse(available_stages(self.member, claim_access(self.member)).exists())
        self.assertNotContains(self.client.get(reverse('core:available_texts')), 'Przejmij</button>', html=False)
        with self.assertRaises(PermissionDenied):
            claim_stage(self.text, stage.stage_type, self.member)
        self.roles('Korektor poskładowy', 'Korektor')
        self.assertFalse(role_access_reason(self.member, role))
        self.assertEqual(list(available_stages(self.member, claim_access(self.member))), [stage])
        self.person.is_active = False
        self.person.save()
        self.assertEqual(self.client.get(route).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(self.client.get(reverse('core:audio_proofreading')).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(route).status_code, 302)


class AssignmentCreationTests(PostLayoutTestData, TestCase):
    def test_creation_permissions_without_pages_and_idempotent_repeat(self):
        self.client.force_login(self.manager)
        data = self.form_data()
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(PostLayoutAssignment.objects.count(), 1)
        item = PostLayoutAssignment.objects.get()
        self.assertEqual(item.status, 'assigned')
        self.assertIsNotNone(item.assigned_start)
        self.assertIsNone(item.work_start)
        self.assertIsNone(item.page_from)
        self.assertEqual(item.pages_display, 'brak zakresu stron')
        self.assertEqual(self.client.post(self.url, self.form_data()).status_code, 302)
        self.assertEqual(PostLayoutAssignment.objects.count(), 2)
        for values in ({'proofreader': self.editor.pk}, {'anthology': ''}):
            self.assertEqual(self.client.post(self.url, self.form_data(**values)).status_code, 400)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(self.url, self.form_data(self.reader)).status_code, 403)
        # Koordynator redakcji widzi formularz bez zakresu stron (trzy karty paska) i dodaje przydział.
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="page_from"')
        document = html.fromstring(page.content)
        self.assertEqual(len(document.xpath('//div[@class="post-layout-toolbar"]/section[@class="card"]')), 3)
        response = self.client.post(self.url, {'action': 'create', 'token': page.context['form'].initial['token'],
                                               'anthology': self.book.pk, 'proofreader': self.reader.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PostLayoutAssignment.objects.count(), 3)

    def test_creation_excludes_ready_and_abandoned_anthologies_and_stale_form(self):
        ready = Anthology.objects.create(title='Fantazmaty 4', status='ready')
        abandoned = Anthology.objects.create(title='Porzucona', status='abandoned')
        form = AssignmentForm(user=self.manager)
        self.assertIn(self.book, form.fields['anthology'].queryset)
        self.assertNotIn(ready, form.fields['anthology'].queryset)
        self.assertNotIn(abandoned, form.fields['anthology'].queryset)
        data = {'anthology': self.book.pk, 'proofreader': self.reader.pk, 'page_from': 1, 'page_to': 2,
                'token': form.initial['token']}
        bound = AssignmentForm(data, user=self.manager)
        self.assertTrue(bound.is_valid(), bound.errors)
        # Antologia oznaczona jako gotowa po wyświetleniu formularza.
        self.book.status = 'ready'
        self.book.save()
        with self.assertRaises(ValidationError):
            create_assignment(user=self.manager, **bound.cleaned_data)
        for book in (ready, abandoned):
            self.assertFalse(AssignmentForm({**data, 'anthology': book.pk}, user=self.manager).is_valid())
        self.assertFalse(PostLayoutAssignment.objects.exists())

    def test_existing_assignment_of_ready_anthology_stays_readable_and_editable(self):
        item = create_assignment(user=self.manager, anthology=self.book, proofreader=self.reader,
                                 page_from=1, page_to=10, token=uuid.uuid4())
        self.book.status = 'ready'
        self.book.save()
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(self.url), self.book.title)
        self.assertEqual(self.client.get(reverse('core:post_layout_edit', args=[item.pk])).status_code, 200)


class AssignmentListTests(PostLayoutTestData, TestCase):
    def test_proofreader_sees_only_own_rows_and_cannot_change_others(self):
        mine = self.create()
        theirs = self.create(proofreader=self.other.pk, page_from=21, page_to=40)
        self.client.force_login(self.reader)
        page = self.client.get(self.url)
        self.assertEqual([x.pk for x in page.context['assignments']], [mine.pk])
        self.assertNotContains(page, 'Dodaj przydział')
        self.assertEqual(self.transition(theirs, 'in_progress').status_code, 403)
        self.assertEqual(self.client.get(self.url, {'assignment': theirs.pk}).context['page_obj'].paginator.count, 0)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url).context['page_obj'].paginator.count, 2)
        self.assertEqual(self.client.get(self.url, {'sort': '-pages'}).context['assignments'][0].pk, theirs.pk)

    def test_row_actions_and_bulk_operations_only_for_coordinator(self):
        item = self.item()
        self.client.force_login(self.manager)
        page = self.client.get(self.url)
        self.assertContains(page, 'Operacje zbiorcze')
        doc = html.fromstring(page.content)
        actions = doc.xpath('//*[contains(@class,"post-layout-row-actions")]')[0]
        self.assertEqual(actions.xpath('.//button/text()'), ['Zapisz'])
        self.assertEqual(actions.xpath('./a/@href'), [reverse('core:post_layout_edit', args=[item.pk])])
        self.assertTrue(doc.xpath('//input[@data-select-table]'))
        self.assertTrue(doc.xpath('//input[@name="selected"][@form="post-layout-bulk"]'))
        self.assertTrue(doc.xpath('//option[@value="delete"]'))
        self.assertContains(page, reverse('core:person_detail', args=[self.reader.person_profile.pk]))
        self.assertContains(page, reverse('core:anthology_detail', args=[self.book.pk]))
        self.assertFalse(doc.xpath('//table//strong'))
        self.client.force_login(self.reader)
        self.assertNotContains(self.client.get(self.url), 'Operacje zbiorcze')
        self.assertEqual(self.client.post(self.url, {'action': 'bulk', 'operation': 'delete'}).status_code, 403)

    def test_profile_and_hide_completed_preserve_open_work(self):
        item = self.create()
        self.transition(item, 'in_progress')
        item.refresh_from_db()
        self.transition(item, 'completed')
        self.create(page_from=21, page_to=40)
        self.client.force_login(self.admin)
        profile_url = reverse('core:person_detail', args=[self.reader.person_profile.pk])
        page = self.client.get(profile_url)
        self.assertEqual(len(page.context['assignments']), 2)
        self.assertFalse(page.context['hide_completed'])
        self.assertEqual({a['kind'] for a in page.context['assignments']}, {'Poskładowa'})
        self.assertContains(page, 'strony 1–20')
        self.assertContains(page, f'?assignment={item.pk}#post-layout-{item.pk}')
        self.assertEqual(len(self.client.get(profile_url, {'hide_completed': '1'}).context['assignments']), 1)
        self.assertEqual(self.client.get(self.url).context['page_obj'].paginator.count, 2)
        self.assertEqual(self.client.get(self.url, {'hide_completed': '1'}).context['page_obj'].paginator.count, 1)
        self.client.force_login(self.editor)
        doc = html.fromstring(self.client.get(profile_url).content)
        self.assertTrue(doc.xpath('//table[@id="person-assignments-table"]//a[contains(@href,"korekta-poskladowa")]'))

    def test_profile_counters_include_post_layout(self):
        self.item()
        _, summary = profile_assignments(self.reader.person_profile, include_authors=False)
        self.assertEqual(summary['reserved'], 1)
