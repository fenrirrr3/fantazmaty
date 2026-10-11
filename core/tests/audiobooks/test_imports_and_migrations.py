"""Importy audiobooków (oczekujących i opublikowanych) oraz migracje danych 0031, 0042 i 0044."""
import copy
import json
import tempfile
from datetime import date
from importlib import import_module
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag
from django.urls import reverse

from core.edit_versions import version_of
from core.models import AudioContributor, Audiobook, AudiobookStage
from fantazmaty.testing import create_member, form_value
from people.models import Person
from texts.models import Anthology, Text
from workflow.models import WorkflowStage


class ImportTestMixin:
    schema = None
    command = None

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_import(self, rows=None, apply=False, **options):
        path = self.root / 'input.json'
        path.write_text(json.dumps({'schema': self.schema, 'records': rows or [self.row]}), encoding='utf-8')
        call_command(self.command, str(path), apply=apply, report=str(self.root / 'report'), stdout=StringIO(),
                     **options)
        return json.loads((self.root / 'report.json').read_text(encoding='utf-8'))


class PendingAudiobookImportTests(ImportTestMixin, TestCase):
    schema = 'fantazmaty-pending-audio-v1'
    command = 'import_pending_audiobooks'

    @classmethod
    def setUpTestData(cls):
        cls.reader = create_member('pending-audio', 'Korektor audiobooków')
        cls.book = Anthology.objects.create(title='Antologia importu', status='ready')
        cls.text = Text.objects.create(title='Tekst importu', anthology=cls.book, length=1)

    def setUp(self):
        super().setUp()
        self.row = {'title': self.text.title, 'anthology': self.book.title, 'status': 'proofreading',
                    'narrator': 'Lektor testowy', 'proofreader': str(self.reader.person_profile), 'engineer': '',
                    'stages': [{'type': 'proofreading', 'started_at': '2026-09-22', 'ended_at': '2026-09-24',
                                'completed': True, 'active': False}]}

    def test_preview_apply_repeat_and_proofreading_finished_without_next_stage(self):
        self.run_import()
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudiobookStage.objects.exists())
        self.run_import(apply=True)
        audio = Audiobook.objects.get()
        self.assertEqual(audio.status, 'corrections')  # po korekcie: Nanoszenie poprawek
        self.assertIsNone(audio.active_stage_id)
        self.assertEqual(AudiobookStage.objects.get().ended_at, date(2026, 9, 24))
        self.assertTrue(AudiobookStage.objects.get().is_completed)
        self.assertEqual(self.run_import(apply=True)['counts']['unchanged_audiobooks'], 1)
        self.assertEqual(AudiobookStage.objects.count(), 1)

    def test_active_stage_unknown_dates_and_no_overwrite_of_published(self):
        self.row['status'] = 'editing'
        self.row['stages'].append({'type': 'editing', 'started_at': None, 'ended_at': None, 'completed': False,
                                   'active': True})
        self.run_import(apply=True)
        audio = Audiobook.objects.get()
        self.assertEqual(audio.active_stage.stage_type, 'editing')
        self.assertIsNone(audio.active_stage.started_at)
        audio.active_stage = None
        audio.status = 'published'
        audio.save()
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'published')

    def test_conflict_rolls_back_other_rows_and_keeps_blacklist(self):
        with self.assertRaises(CommandError):
            self.run_import([self.row, {**self.row, 'title': 'Nieistniejący tekst'}], apply=True)
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudiobookStage.objects.exists())
        self.text.audiobook_blacklisted = True
        self.text.save()
        # Dawny status „czeka na publikację” z zakończonym montażem trafia do Opublikowanych.
        self.row['status'] = 'awaiting_publication'
        self.row['stages'].append({'type': 'editing', 'started_at': None, 'ended_at': '2026-09-29', 'completed': True,
                                   'active': False})
        result = self.run_import(apply=True)
        self.assertTrue(result['warnings'])
        self.assertEqual(Audiobook.objects.get().status, 'published')
        self.assertEqual(AudiobookStage.objects.get(stage_type='editing').ended_at, date(2026, 9, 29))
        self.text.refresh_from_db()
        self.assertTrue(self.text.audiobook_blacklisted)


class PublishedAudiobookImportTests(ImportTestMixin, TestCase):
    schema = 'fantazmaty-published-audio-v1'
    command = 'import_published_audiobooks'

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('admin-import-audio', 'a@example.test', 'test')
        cls.reader = get_user_model().objects.create_user('historical-reader', first_name='Anna', last_name='Koral',
                                                          is_active=False)
        Person.objects.create(first_name='Anna', last_name='Koral', user=cls.reader, is_active=False)
        cls.book = Anthology.objects.create(title='Antologia', status=Anthology.Status.READY)
        cls.text = Text.objects.create(anthology=cls.book, title='Opowiadanie', length=123)

    def setUp(self):
        super().setUp()
        self.row = {
            'anthology': 'Antologia', 'title': 'Opowiadanie', 'status': 'published', 'narrator': 'Lektor',
            'engineer': 'Dźwiękowiec', 'proofreader': 'Anna Koral', 'premiere_date': '2024-01-03',
            'publications': [
                {'service': 'youtube', 'part': 1, 'url': 'https://www.youtube.com/watch?v=first', 'date': '2024-01-04'},
                {'service': 'hearthis', 'part': 1, 'url': 'https://hearthis.at/fantazmaty/first/', 'date': '2024-01-03'},
                {'service': 'youtube', 'part': 2, 'url': 'https://www.youtube.com/watch?v=second', 'date': '2024-01-10'},
                {'service': 'hearthis', 'part': 2, 'url': 'https://hearthis.at/fantazmaty/second/', 'date': '2024-01-10'}],
            'proofreading': [{'started_at': '2024-01-01', 'ended_at': '2024-01-02', 'proofreader': 'Anna Koral'}]}

    def test_dry_run_apply_repeat_and_no_editorial_or_identity_changes(self):
        before = version_of(self.text)
        self.assertEqual(self.run_import()['counts']['new_audiobooks'], 1)
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
        mapping = self.root / 'people.json'
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
                        {'proofreading': [{'started_at': '2024-01-05', 'ended_at': '2024-01-01',
                                           'proofreader': 'Anna Koral'}]},
                        {'publications': [{'service': 'youtube', 'part': 1, 'url': 'javascript:alert(1)',
                                           'date': '2024-01-03'}]}):
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
        response = self.client.post(url, {
            'narrator_name': 'Nowy lektor', 'narrator_email': 'new@example.test',
            'engineer_name': 'Nowy dźwiękowiec', 'engineer_email': 'sound@example.test',
            'proofreader': self.reader.pk, 'additional_links': json.dumps(audio.additional_links),
            '_edit_version': form_value(self.client.get(url)), '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.narrator_name, 'Nowy lektor')
        self.assertEqual(audio.narrator_email, 'new@example.test')
        self.assertEqual(audio.engineer_name, 'Nowy dźwiękowiec')
        self.assertEqual(audio.engineer_email, 'sound@example.test')
        response = self.client.post(url, {'narrator_name': 'Nowy lektor', 'engineer_name': 'Nowy dźwiękowiec',
                                          'additional_links': '', '_edit_version': form_value(self.client.get(url)),
                                          '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.additional_links, [])
        audio.additional_links = [{'service': 'youtube', 'part': 2, 'url': 'javascript:evil'}]
        with self.assertRaises(ValidationError):
            audio.clean_fields()


@tag('migration')
class AudioContactMigrationTests(TestCase):
    def test_0031_existing_audio_contacts_are_idempotent_and_preserve_work(self):
        member = create_member('migration-member', 'Redaktor')
        member.email = 'migration63@example.test'
        member.save(update_fields=['email'])
        book = Anthology.objects.create(title='Antologia migracji kontaktów', status='ready')
        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps  # bieżący schemat bazy testowej
        Audio = apps.get_model('core', 'Audiobook')
        rows = []
        for i in range(2):
            story = Text.objects.create(title=f'Stare nagranie {i}', anthology=book, length=1)
            rows.append(Audio.objects.create(text_id=story.pk, narrator_name='Wspólny lektor',
                                             narrator_email=member.email, status='published',
                                             premiere_date='2026-01-02'))
        populate = import_module('core.migrations.0031_populate_audio_contacts').populate
        for _ in range(2):
            populate(apps, SimpleNamespace(connection=connection))
        contacts = AudioContributor.objects.filter(name='Wspólny lektor')
        self.assertEqual(contacts.count(), 1)
        self.assertEqual(contacts.get().user_id, member.pk)
        for audio in rows:
            audio.refresh_from_db()
            self.assertEqual(audio.narrator_contact_id, contacts.get().pk)
            self.assertEqual(audio.status, 'published')
            self.assertEqual(str(audio.premiere_date), '2026-01-02')


@tag('migration')
class AudiobookWorkflowMigrationTests(TransactionTestCase):
    def migrate_from(self, target):
        """Cofa bazę do `target`, zwraca (apps sprzed migracji, docelowe węzły); tearDown przywraca schemat."""
        after = MigrationExecutor(connection).loader.graph.leaf_nodes()
        MigrationExecutor(connection).migrate([('core', target)])
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(after))
        return MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps, after

    def migrate_forward(self, after):
        MigrationExecutor(connection).migrate(after)
        apps = MigrationExecutor(connection).loader.project_state(after).apps
        return apps.get_model('core', 'Audiobook'), apps.get_model('core', 'AudiobookStage'), apps

    def test_0042_legacy_rows_become_stages_without_invented_dates(self):
        apps, after = self.migrate_from('0041_audiobook_workflow_fields')
        Audio = apps.get_model('core', 'Audiobook')
        Stage = apps.get_model('core', 'AudiobookStage')
        book = apps.get_model('texts', 'Anthology').objects.create(title='Migracja audiobooków', status='ready')
        Text_ = apps.get_model('texts', 'Text')
        made = {}

        def audio(name, status, stages=(), active=None, **fields):
            text = Text_.objects.create(title=name, anthology=book, length=1)
            row = Audio.objects.create(text=text, status=status, **fields)
            created = [Stage.objects.create(text=text, stage_type=kind, started_at=start, ended_at=end,
                                            is_completed=done) for kind, start, end, done in stages]
            if active is not None:
                row.active_stage = created[active]
                row.save()
            made[name] = text.pk

        audio('opublikowany', 'published', premiere_date=date(2025, 3, 1), youtube_url='https://youtu.be/a')
        audio('publikacja jako etap', 'published', [('published', date(2025, 4, 2), None, True)])
        audio('czeka na publikację', 'awaiting_publication', [
            ('recording', None, date(2025, 11, 18), True), ('editing', None, date(2026, 9, 29), True)])
        audio('montaż trwa', 'editing', [('recording', None, None, True), ('editing', None, None, False)], active=1)
        audio('korekta zakończona', 'proofreading', [('proofreading', None, None, True)])
        audio('poprawki trwają', 'corrections', [('corrections', None, None, False)], active=0)
        audio('korekta trwa', 'proofreading', [('proofreading', date(2026, 9, 20), None, False)], active=0)
        audio('montaż zakończony', 'editing')
        audio('nagranie z kolumny', 'recording', recording_started_at=date(2025, 1, 2))
        audio('etap oczekiwania na publikację', 'awaiting_publication', [
            ('editing', date(2026, 8, 1), None, False), ('awaiting_publication', date(2026, 8, 10), None, False)],
            active=1)
        audio('korekta bez korektora', 'proofreading', [('recording', None, None, True),
                                                        ('proofreading', None, None, False)], active=1)
        Audio, Stage, apps = self.migrate_forward(after)

        def state(name):
            row = Audio.objects.get(text_id=made[name])
            stages = [(s.stage_type, s.started_at, s.ended_at, s.is_completed, s.start_unknown)
                      for s in Stage.objects.filter(text_id=made[name]).order_by('position', 'pk')]
            return row, stages

        row, stages = state('opublikowany')
        self.assertEqual((row.status, stages), ('published', []))
        row, stages = state('publikacja jako etap')
        self.assertEqual((row.status, row.premiere_date, stages), ('published', date(2025, 4, 2), []))
        row, stages = state('czeka na publikację')
        self.assertEqual((row.status, row.active_stage_id), ('published', None))
        self.assertEqual([s[0] for s in stages], ['recording', 'editing'])
        row, stages = state('montaż trwa')
        self.assertEqual(row.status, 'editing')
        self.assertEqual(stages[-1], ('editing', None, None, False, True))
        row, stages = state('korekta zakończona')
        self.assertEqual((row.status, stages[-1]), ('corrections', ('corrections', None, None, False, False)))
        row, stages = state('poprawki trwają')
        self.assertEqual((row.status, stages), ('corrections', [('corrections', None, None, False, True)]))
        row, _ = state('korekta trwa')
        self.assertEqual(row.status, 'proofreading')
        row, stages = state('montaż zakończony')
        self.assertEqual((row.status, stages), ('published', [('editing', None, None, True, False)]))
        row, stages = state('nagranie z kolumny')
        self.assertEqual(row.status, 'proofreading')
        self.assertEqual(stages, [('recording', date(2025, 1, 2), None, True, False),
                                  ('proofreading', None, None, False, False)])
        row, stages = state('etap oczekiwania na publikację')
        self.assertEqual((row.status, stages),
                         ('published', [('editing', date(2026, 8, 1), date(2026, 8, 10), True, False)]))
        row, stages = state('korekta bez korektora')
        self.assertEqual((row.status, stages[-1]), ('proofreading', ('proofreading', None, None, False, False)))
        # Ponowne uruchomienie konwersji niczego nie zmienia.
        before = {name: state(name)[1] for name in made}
        import_module('core.migrations.0044_audiobook_linear_stages').convert(apps, None)
        self.assertEqual({name: state(name)[1] for name in made}, before)

    def test_0044_statuses_from_previous_version_collapse_to_fixed_order(self):
        """Statusy pośrednie i wstrzymanie z wersji 0043 przechodzą na sześć statusów."""
        apps, after = self.migrate_from('0043_remove_audiobook_stage_dates')
        Audio = apps.get_model('core', 'Audiobook')
        Stage = apps.get_model('core', 'AudiobookStage')
        book = apps.get_model('texts', 'Anthology').objects.create(title='Migracja 0044', status='ready')
        Text_ = apps.get_model('texts', 'Text')
        made = {}

        def audio(name, status, stages=(), active=None, **fields):
            text = Text_.objects.create(title=name, anthology=book, length=1)
            row = Audio.objects.create(text=text, status=status, **fields)
            created = [Stage.objects.create(text=text, stage_type=kind, position=n, started_at=start,
                                            ended_at=end, is_completed=done, start_unknown=unknown)
                       for n, (kind, start, end, done, unknown) in enumerate(stages, start=1)]
            if active is not None:
                row.active_stage = created[active]
                row.save()
            made[name] = text.pk

        audio('czeka na nagranie', 'recording_waiting', [('recording', None, None, False, False)], active=0)
        audio('po poprawkach', 'after_corrections', [('recording', None, None, True, False),
                                                     ('proofreading', None, None, True, False),
                                                     ('corrections', None, date(2026, 9, 1), True, False)])
        audio('wstrzymane', 'paused', [('recording', date(2026, 9, 2), None, False, False)],
              paused_at=date(2026, 9, 5))
        audio('stary etap', 'pending', [('pending', None, None, True, False)])
        audio('oczekuje na publikację', 'awaiting_publication', [('editing', None, None, True, True)])
        Audio, Stage, _ = self.migrate_forward(after)

        def state(name):
            row = Audio.objects.get(text_id=made[name])
            stages = [(s.stage_type, s.queued_at, s.is_completed)
                      for s in Stage.objects.filter(text_id=made[name]).order_by('position', 'pk')]
            current = Stage.objects.get(pk=row.active_stage_id).stage_type if row.active_stage_id else None
            return row.status, current, stages

        self.assertEqual(state('czeka na nagranie'), ('pending', None, []))
        status, current, stages = state('po poprawkach')
        self.assertEqual((status, current, stages[-1]), ('editing', 'editing', ('editing', date(2026, 9, 1), False)))
        self.assertEqual(state('wstrzymane')[:2], ('recording', 'recording'))
        self.assertEqual(state('stary etap'), ('pending', None, []))
        self.assertEqual(state('oczekuje na publikację')[:2], ('published', None))
