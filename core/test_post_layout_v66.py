from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.models import Audiobook, AudiobookStage, PostLayoutAssignment
from core.post_layout import AssignmentForm, create_assignment
from texts.models import Anthology, Text
from workflow.tests import create_member


class PostLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('layout66admin', 'layout66@example.test', 'test')
        cls.manager = create_member('layout66manager', 'Koordynator korekty')
        cls.reader = create_member('layout66reader', 'Korektor poskładowy')
        cls.other = create_member('layout66other', 'Korektor poskładowy')
        cls.editor = create_member('layout66editor', 'Koordynator redakcji')
        cls.book = Anthology.objects.create(title='Antologia poskładowa')
        cls.url = reverse('core:post_layout')

    def form_data(self, user=None, **values):
        user = user or self.manager
        form = AssignmentForm(user=user)
        return {'action': 'create', 'token': form.initial['token'], 'anthology': self.book.pk,
            'proofreader': self.reader.pk, 'page_from': 1, 'page_to': 20, **values}

    def create(self, user=None, **values):
        user = user or self.manager
        form = AssignmentForm(self.form_data(user, **values), user=user)
        self.assertTrue(form.is_valid(), form.errors)
        return create_assignment(user=user, **form.cleaned_data)

    def transition(self, item, status, user=None, version=None):
        self.client.force_login(user or self.reader)
        return self.client.post(self.url, {'action': 'status', 'pk': item.pk, 'status': status,
            'version': item.version if version is None else version})

    def test_creation_permissions_ranges_and_idempotent_repeat(self):
        self.client.force_login(self.manager)
        data = self.form_data()
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(PostLayoutAssignment.objects.count(), 1)
        item = PostLayoutAssignment.objects.get()
        self.assertEqual(item.status, 'assigned')
        self.assertIsNotNone(item.assigned_start)
        self.assertIsNone(item.work_start)
        self.assertEqual(self.client.post(self.url, self.form_data(page_from=21, page_to=50)).status_code, 302)
        self.assertEqual(PostLayoutAssignment.objects.count(), 2)
        for values in ({'page_from': 0}, {'page_from': 30, 'page_to': 20}, {'proofreader': self.editor.pk}):
            self.assertEqual(self.client.post(self.url, self.form_data(**values)).status_code, 400)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(self.url, self.form_data(self.reader)).status_code, 403)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_transitions_record_stage_dates_and_reject_stale_skip_and_reverse(self):
        item = self.create()
        old_version = item.version
        self.assertEqual(self.transition(item, 'completed').status_code, 400)
        # Use dates after the initial assignment so the database chronology constraint is meaningful.
        with patch('core.post_layout.timezone.localdate', return_value=item.assigned_start):
            self.assertEqual(self.transition(item, 'in_progress').status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.assigned_end, item.work_start)
        self.assertIsNone(item.work_end)
        self.assertIsNone(item.completed_on)
        self.assertEqual(self.transition(item, 'completed', version=old_version).status_code, 409)
        self.assertEqual(self.transition(item, 'assigned').status_code, 400)
        self.assertEqual(self.transition(item, 'completed').status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.work_end, item.completed_on)
        self.assertEqual(item.status, 'completed')
        self.assertEqual(self.transition(item, 'in_progress').status_code, 400)

    def test_own_rows_only_and_other_person_cannot_change_status(self):
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

    def test_admin_and_database_guards(self):
        item = self.create()
        self.client.force_login(self.admin)
        page = self.client.get(reverse('admin:core_postlayoutassignment_change', args=[item.pk]))
        self.assertContains(page, 'Otwórz Korektę poskładową')
        self.assertNotContains(page, 'name="status"')
        self.assertContains(self.client.get(reverse('admin:index')), 'Korekta poskładowa')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PostLayoutAssignment.objects.filter(pk=item.pk).update(page_to=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PostLayoutAssignment.objects.filter(pk=item.pk).update(status='completed')
        person = self.reader.person_profile
        person.is_external = True
        person.save()
        form = AssignmentForm(self.form_data(), user=self.manager)
        self.assertFalse(form.is_valid())

    def test_audio_hide_completed_respects_performer_and_repeated_stage(self):
        old = create_member('layout66audio-old', 'Korektor audiobooków')
        new = create_member('layout66audio-new', 'Korektor audiobooków')
        text = Text.objects.create(title='Wznowiona korekta audio', anthology=self.book, length=1)
        audio = Audiobook.objects.create(text=text, status='proofreading', proofreader=new)
        AudiobookStage.objects.create(text=text, stage_type='proofreading', performer=old, is_completed=True,
            started_at=date(2026, 1, 1), ended_at=date(2026, 1, 2))
        stage = AudiobookStage.objects.create(text=text, stage_type='proofreading', performer=new, started_at=date(2026, 1, 3))
        audio.active_stage = stage
        audio.save()
        queue = reverse('core:audio_proofreading')
        for user, count in ((old, 0), (new, 1), (self.admin, 1)):
            self.client.force_login(user)
            page = self.client.get(queue)
            self.assertFalse(page.context['hide_completed'])
            self.assertEqual(page.context['page_obj'].paginator.count, 1)
            self.assertEqual(self.client.get(queue, {'hide_completed': '1'}).context['page_obj'].paginator.count, count)
        stage.is_completed = True
        stage.ended_at = date(2026, 1, 4)
        stage.save()
        audio.active_stage = None
        audio.status = 'published'
        audio.save()
        self.assertEqual(self.client.get(queue, {'hide_completed': '1'}).context['page_obj'].paginator.count, 0)
        for user in (old, new):
            profile = reverse('core:person_detail', args=[user.person_profile.pk])
            self.assertEqual(len(self.client.get(profile).context['assignments']), 1)
            self.assertEqual(len(self.client.get(profile, {'hide_completed': '1'}).context['assignments']), 0)
