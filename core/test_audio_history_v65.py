from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage
from core.selectors.people import profile_assignments
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.tests import create_member


class AudioHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('hist-admin', 'hist-admin@example.test', 'test')
        cls.old = create_member('hist-old', 'Korektor audiobooków')
        cls.new = create_member('hist-new', 'Korektor audiobooków')
        cls.coordinator = create_member('hist-coord', 'Koordynator korekty')
        cls.book = Anthology.objects.create(title='Dawne nagrania', status='ready')
        cls.text = Text.objects.create(title='Nagranie z dwiema korektami', anthology=cls.book, length=1)
        cls.audio = Audiobook.objects.create(text=cls.text, status='published', proofreader=cls.new)
        cls.day = timezone.localdate() - timedelta(days=10)
        cls.first = AudiobookStage.objects.create(text=cls.text, stage_type='proofreading',
            performer=cls.old, started_at=cls.day, ended_at=cls.day + timedelta(days=2), is_completed=True)
        cls.second = AudiobookStage.objects.create(text=cls.text, stage_type='proofreading',
            performer=cls.new, started_at=cls.day + timedelta(days=3), is_completed=True)
        cls.queue = reverse('core:audio_proofreading')

    def test_history_visibility_filtering_and_no_duplicate_story_rows(self):
        for user, expected in [(self.old, [self.first.pk]), (self.new, [self.second.pk]),
                (self.coordinator, [self.second.pk, self.first.pk]), (self.admin, [self.second.pk, self.first.pk])]:
            self.client.force_login(user)
            page = self.client.get(self.queue)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.context['page_obj'].paginator.count, 1)
            row = list(page.context['texts'])[0]
            self.assertEqual([s['pk'] for s in row['corrections']], expected)
            self.assertFalse(row['allow_assignment'])
            self.assertNotContains(page, 'name="proofreader"')
            self.assertContains(page, reverse('core:audiobook_detail', args=[self.text.pk]))
            self.assertEqual(self.client.get(self.queue, {'status': 'editing'}).context['page_obj'].paginator.count, 0)
            self.assertEqual(self.client.get(self.queue, {'status': 'published', 'q': self.text.title}).context['page_obj'].paginator.count, 1)
        outsider = create_member('hist-outsider', 'Korektor audiobooków')
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.queue).context['page_obj'].paginator.count, 0)

    def test_blacklisted_withdrawn_and_disabled_remain_read_only_in_history(self):
        self.text.for_recording = False
        self.text.audiobook_blacklisted = True
        self.text.save()
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.client.force_login(self.admin)
        page = self.client.get(self.queue)
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertContains(page, 'Wyłączony z produkcji')
        self.assertFalse(list(page.context['texts'])[0]['allow_assignment'])
        self.assertEqual(self.client.get(reverse('core:external_audiobooks')).context['page_obj'].paginator.count, 0)

    def test_profile_keeps_actual_performer_and_uses_audio_links(self):
        person = self.old.person_profile
        person.is_active = False
        person.save()
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:person_detail', args=[person.pk]))
        self.assertEqual(page.status_code, 200)
        rows = page.context['assignments']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'Audiobook')
        self.assertEqual(rows[0]['latest_stage']['pk'], self.first.pk)
        self.assertTrue(rows[0]['has_completed_work'])
        self.assertEqual(rows[0]['latest_stage']['ended_at'], self.first.ended_at)
        self.assertIsNone(rows[0]['assigned_at'])
        doc = html.fromstring(page.content)
        table = doc.get_element_by_id('person-assignments-table')
        self.assertIn('Rodzaj', table.xpath('.//th/text()'))
        self.assertNotIn('Etap', table.xpath('.//th/text()'))
        self.assertEqual(set(table.xpath('.//tbody//a[contains(@href,"audiobooki/")]/@href')),
            {reverse('core:audiobook_detail', args=[self.text.pk])})

    def test_text_assignments_remain_and_missing_history_is_not_invented(self):
        WorkflowRoleAssignment.objects.create(text=self.text, role='editor', assigned_to=self.new)
        rows, summary = profile_assignments(self.new.person_profile, include_authors=True)
        self.assertEqual({row['kind'] for row in rows}, {'Tekst', 'Audiobook'})
        audio = next(row for row in rows if row['kind'] == 'Audiobook')
        self.assertIsNone(audio['latest_stage']['ended_at'])
        unrelated = Text.objects.create(title='Samo przypisanie przed korektą', anthology=self.book, length=1)
        Audiobook.objects.create(text=unrelated, status='recording', proofreader=self.old)
        rows, _ = profile_assignments(self.old.person_profile, include_authors=True)
        self.assertEqual([row['text']['pk'] for row in rows], [self.text.pk])

    def test_current_reassignment_does_not_credit_previous_person(self):
        active = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', performer=self.old, started_at=timezone.localdate())
        self.audio.status = 'proofreading'
        self.audio.active_stage = active
        self.audio.save()
        for user, expected in [(self.old, {self.first.pk}), (self.new, {self.second.pk, active.pk})]:
            rows, _ = profile_assignments(user.person_profile, include_authors=True)
            self.assertEqual({r['latest_stage']['pk'] for r in rows}, expected)
        self.client.force_login(self.admin)
        self.assertTrue(list(self.client.get(self.queue).context['texts'])[0]['allow_assignment'])

    def test_completed_correction_cannot_be_reassigned_with_forged_post(self):
        self.audio.status = 'proofreading'
        self.audio.save()
        self.client.force_login(self.admin)
        token = signing.dumps([self.admin.pk, f'texts.text:{self.text.pk}', version_of(self.text)], salt='cms-edit-version')
        response = self.client.post(reverse('core:assign_audio_proofreader', args=[self.text.pk]),
            {'proofreader': self.old.pk, '_edit_version': token})
        self.assertEqual(response.status_code, 403)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.proofreader_id, self.new.pk)
        self.second.refresh_from_db()
        self.assertEqual(self.second.performer_id, self.new.pk)

    def test_text_preview_shows_real_audio_status_without_creating_record(self):
        self.client.force_login(self.admin)
        url = reverse('core:assigned_text_detail', args=[self.text.pk])
        for value, label in Audiobook.Status.choices:
            self.audio.status = value
            self.audio.save(update_fields=['status'])
            doc = html.fromstring(self.client.get(url).content)
            section = doc.get_element_by_id('text-audiobook')
            self.assertIn(label, section.text_content())
        empty = Text.objects.create(title='Bez produkcji', anthology=self.book, length=1)
        page = self.client.get(reverse('core:assigned_text_detail', args=[empty.pk]))
        self.assertContains(page, 'Do nagrania')
        self.assertFalse(Audiobook.objects.filter(text=empty).exists())
