import copy
import json
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage
from people.models import Person
from texts.models import Anthology, Text
from workflow.models import WorkflowStage


class PublishedAudiobookImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('admin-import-audio', 'a@example.test', 'test')
        cls.reader = get_user_model().objects.create_user('historical-reader', first_name='Anna', last_name='Koral', is_active=False)
        Person.objects.create(first_name='Anna', last_name='Koral', user=cls.reader, is_active=False)
        cls.book = Anthology.objects.create(title='Antologia', status=Anthology.Status.READY)
        cls.text = Text.objects.create(anthology=cls.book, title='Opowiadanie', length=123)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = {'anthology': 'Antologia', 'title': 'Opowiadanie', 'status': 'published',
            'narrator': 'Lektor', 'engineer': 'Dźwiękowiec', 'proofreader': 'Anna Koral',
            'premiere_date': '2024-01-03', 'publications': [
                {'service': 'youtube', 'part': 1, 'url': 'https://www.youtube.com/watch?v=first', 'date': '2024-01-04'},
                {'service': 'hearthis', 'part': 1, 'url': 'https://hearthis.at/fantazmaty/first/', 'date': '2024-01-03'},
                {'service': 'youtube', 'part': 2, 'url': 'https://www.youtube.com/watch?v=second', 'date': '2024-01-10'},
                {'service': 'hearthis', 'part': 2, 'url': 'https://hearthis.at/fantazmaty/second/', 'date': '2024-01-10'}],
            'proofreading': [{'started_at': '2024-01-01', 'ended_at': '2024-01-02', 'proofreader': 'Anna Koral'}]}

    def run_import(self, rows=None, apply=False, **kwargs):
        path = self.root/'input.json'
        path.write_text(json.dumps({'schema': 'fantazmaty-published-audio-v1', 'records': rows or [self.row]}), encoding='utf-8')
        call_command('import_published_audiobooks', str(path), apply=apply,
            report=str(self.root/'report'), stdout=StringIO(), **kwargs)
        return json.loads((self.root/'report.json').read_text(encoding='utf-8'))

    def test_dry_run_apply_repeat_and_no_editorial_or_identity_changes(self):
        before = version_of(self.text)
        report = self.run_import()
        self.assertEqual(report['counts']['new_audiobooks'], 1)
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudiobookStage.objects.exists())
        self.assertEqual(version_of(self.text), before)
        self.run_import(apply=True)
        audio = Audiobook.objects.get(text=self.text)
        self.assertEqual(audio.premiere_date, date(2024, 1, 3))
        self.assertEqual(audio.proofreader, self.reader)
        self.assertEqual(audio.status, 'published')
        self.assertIsNone(audio.active_stage)
        stage = AudiobookStage.objects.get()
        self.assertEqual(stage.performer, self.reader)
        self.assertTrue(stage.is_completed)
        self.assertEqual(stage.ended_at, date(2024, 1, 2))
        before = version_of(self.text)
        self.assertEqual(self.run_import(apply=True)['counts']['unchanged_audiobooks'], 1)
        self.assertEqual(AudiobookStage.objects.count(), 1)
        self.assertEqual(version_of(self.text), before)
        self.assertFalse(WorkflowStage.objects.exists())
        self.reader.refresh_from_db()
        self.assertFalse(self.reader.is_active)
        self.assertFalse(self.reader.person_profile.roles.exists())
        self.client.force_login(self.admin)
        for url in (reverse('core:audiobooks'), reverse('core:audiobook_detail', args=[self.text.pk])):
            page = self.client.get(url)
            for link in self.row['publications']:
                self.assertContains(page, link['url'])

    def test_missing_text_or_ambiguous_person_rolls_back_everything(self):
        missing = {**copy.deepcopy(self.row), 'title': 'Nie ma'}
        with self.assertRaises(CommandError):
            self.run_import([self.row, missing], apply=True)
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudiobookStage.objects.exists())
        get_user_model().objects.create_user('other', first_name='Anna', last_name='Koral')
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        self.assertFalse(Audiobook.objects.exists())
        mapping = self.root/'people.json'
        mapping.write_text(json.dumps({'Anna Koral': self.reader.pk}))
        self.run_import(apply=True, people_map=str(mapping))
        self.assertEqual(Audiobook.objects.get().proofreader_id, self.reader.pk)

    def test_active_work_is_not_overwritten_and_blacklist_is_preserved(self):
        audio = Audiobook.objects.create(text=self.text, status='recording')
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'recording')
        audio.status = 'pending'
        audio.save()
        self.text.audiobook_blacklisted = True
        self.text.save()
        self.run_import(apply=True)
        self.text.refresh_from_db()
        self.assertTrue(self.text.audiobook_blacklisted)
        self.assertFalse(self.text.for_recording)

    def test_wrong_dates_duplicate_rows_and_unsafe_links_roll_back(self):
        for updates in ({'premiere_date': '2024-01-04'}, {'status': 'recording'},
                {'proofreading': [{'started_at': '2024-01-05', 'ended_at': '2024-01-01', 'proofreader': 'Anna Koral'}]},
                {'publications': [{'service': 'youtube', 'part': 1, 'url': 'javascript:alert(1)', 'date': '2024-01-03'}]}):
            with self.subTest(updates=updates), self.assertRaises(CommandError):
                self.run_import([{**self.row, **updates}], apply=True)
            self.assertFalse(Audiobook.objects.exists())
            self.assertFalse(AudiobookStage.objects.exists())
        with self.assertRaises(CommandError):
            self.run_import([self.row, self.row], apply=True)
        self.assertFalse(Audiobook.objects.exists())

    def test_existing_history_and_contacts_are_preserved(self):
        audio = Audiobook.objects.create(text=self.text, narrator_name='Lektor', narrator_email='n@example.test',
            engineer_email='e@example.test', engineer_name='Dźwiękowiec')
        old = AudiobookStage.objects.create(text=self.text, stage_type='editing', is_completed=True)
        same = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', is_completed=True,
            started_at=date(2024, 1, 1), ended_at=date(2024, 1, 2))
        self.run_import(apply=True)
        self.assertEqual(AudiobookStage.objects.count(), 2)
        same.refresh_from_db()
        self.assertEqual(same.performer, self.reader)
        self.assertTrue(AudiobookStage.objects.filter(pk=old.pk).exists())
        audio.refresh_from_db()
        self.assertEqual(audio.narrator_email, 'n@example.test')
        self.assertEqual(audio.engineer_email, 'e@example.test')
        same.ended_at = date(2024, 1, 3)
        same.save()
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        self.assertEqual(AudiobookStage.objects.count(), 2)

    def test_admin_edits_names_emails_and_validates_additional_links(self):
        self.run_import(apply=True)
        audio = Audiobook.objects.get()
        self.client.force_login(self.admin)
        url = reverse('admin:core_audiobook_change', args=[audio.pk])
        page = self.client.get(url)
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(url, {'narrator_name': 'Nowy lektor', 'narrator_email': 'new@example.test',
            'engineer_name': 'Nowy dźwiękowiec', 'engineer_email': 'sound@example.test',
            'proofreader': self.reader.pk, 'additional_links': json.dumps(audio.additional_links),
            '_edit_version': token, '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.narrator_name, 'Nowy lektor')
        self.assertEqual(audio.narrator_email, 'new@example.test')
        self.assertEqual(audio.engineer_name, 'Nowy dźwiękowiec')
        self.assertEqual(audio.engineer_email, 'sound@example.test')
        page = self.client.get(url)
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(url, {'narrator_name': 'Nowy lektor', 'engineer_name': 'Nowy dźwiękowiec',
            'additional_links': '', '_edit_version': token, '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.additional_links, [])
        audio.additional_links = [{'service': 'youtube', 'part': 2, 'url': 'javascript:evil'}]
        with self.assertRaises(ValidationError):
            audio.clean_fields()
