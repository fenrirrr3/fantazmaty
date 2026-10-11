"""Importy ilustracji (kredyty i tabele ilustracji) oraz scalanie zdublowanego ilustratora:
podgląd, zapis, idempotencja, wycofanie całości przy konflikcie i ochrona istniejących danych."""
import json
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from authors.models import Author
from core.edit_versions import version_of
from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


class ImportFilesMixin:
    """Pliki wejściowy i raportu w katalogu tymczasowym."""

    def setUp(self):
        super().setUp()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.input = Path(tmp.name) / 'data.json'
        self.report = Path(tmp.name) / 'report.json'

    def run_command(self, command, payload, apply):
        self.input.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        call_command(command, str(self.input), apply=apply, report=str(self.report), stdout=StringIO())
        return json.loads(self.report.read_text(encoding='utf-8'))


class IllustrationCreditsImportTests(ImportFilesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Testowa książka', status='ready')
        cls.story = Text.objects.create(title='Jeden tekst', anthology=cls.book, length=20)
        cls.author = Author.objects.create(first_name='Maria', last_name='Autor')
        cls.story.authors.add(cls.author)

    def setUp(self):
        super().setUp()
        self.row = {'anthology': self.book.title, 'title': self.story.title, 'author': 'Maria Autor',
                    'illustrators': ['Anna Artysta', 'Graphos']}

    def run_import(self, *, apply=False, rows=None, aliases=None):
        payload = {'schema_version': 1, 'texts': rows if rows is not None else [self.row],
                   'illustrator_aliases': aliases or {}}
        return self.run_command('import_illustration_credits', payload, apply)

    def test_preview_rolls_back_apply_is_idempotent_and_dates_unknown(self):
        preview = self.run_import()
        self.assertFalse(Illustration.objects.filter(text=self.story).exists())
        self.assertFalse(Illustrator.objects.exists())
        self.book.refresh_from_db()
        self.assertFalse(self.book.has_illustrations)
        self.assertEqual(preview['counts']['created'], 1)
        users = get_user_model().objects.count()
        saved = self.run_import(apply=True)
        row = Illustration.objects.get(text=self.story)
        self.assertEqual(row.illustrators.count(), 2)
        self.assertEqual(row.status, 'delivered')
        self.assertIsNone(row.assigned_at)
        self.assertFalse(Illustrator.objects.filter(is_active=True).exists())
        self.assertEqual(get_user_model().objects.count(), users)
        self.book.refresh_from_db()
        self.assertTrue(self.book.has_illustrations)
        again = self.run_import(apply=True)
        self.assertEqual(again['counts']['unchanged'], 1)
        self.assertEqual(Illustrator.objects.count(), 2)
        self.assertEqual(saved['mode'], 'zapisano')

    def test_conflict_late_in_batch_rolls_back_every_change_and_writes_report(self):
        with self.assertRaises(CommandError):
            self.run_import(apply=True, rows=[self.row, {**self.row, 'title': 'Brak tekstu'}])
        self.assertFalse(Illustrator.objects.exists())
        self.assertFalse(Illustration.objects.exists())
        self.book.refresh_from_db()
        self.assertFalse(self.book.has_illustrations)
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertTrue(report['conflicts'])
        self.assertIn('nic nie zapisano', report['mode'])

    def test_existing_notes_preserved_and_single_artist_extended(self):
        artist = Illustrator.objects.create(first_name='Anna', last_name='Artysta', is_active=True)
        illustration = Illustration(text=self.story, coordinator_notes='Zachowaj tę notatkę',
                                    story_url='https://example.org/story')
        illustration.set_artists([artist], status='delivered', preserve_assignment_date=True)
        self.run_import(apply=True)
        illustration.refresh_from_db()
        self.assertEqual(illustration.coordinator_notes, 'Zachowaj tę notatkę')
        self.assertEqual(illustration.story_url, 'https://example.org/story')
        self.assertIsNone(illustration.assigned_at)
        self.assertEqual(illustration.illustrators.count(), 2)
        artist.refresh_from_db()
        self.assertTrue(artist.is_active)

    def test_active_work_and_manual_credit_are_never_overwritten(self):
        illustration = Illustration.objects.create(text=self.story, manual_illustrator_name='Ręczny wpis',
                                                   status='assigned')
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        illustration.refresh_from_db()
        self.assertEqual(illustration.manual_illustrator_name, 'Ręczny wpis')
        self.assertFalse(Illustrator.objects.exists())

    def test_explicit_alias_reuses_profile_without_changing_its_name(self):
        contact = Illustrator.objects.create(first_name='Anna', last_name='Artysta')
        row = {**self.row, 'illustrators': ['Pseudonim']}
        self.run_import(apply=True, rows=[row], aliases={'Pseudonim': 'Anna Artysta'})
        self.assertEqual(Illustrator.objects.count(), 1)
        self.assertEqual(list(Illustration.objects.get(text=self.story).illustrators.all()), [contact])

    def test_missing_credit_is_skipped_without_creating_anything(self):
        report = self.run_import(apply=True, rows=[
            {**self.row, 'illustrators': [], 'skip_reason': 'Nieznany ilustrator'}])
        self.assertEqual(report['counts']['skipped'], 1)
        self.assertFalse(Illustrator.objects.exists())
        self.assertFalse(Illustration.objects.exists())


class IllustrationTableImportTests(ImportFilesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.book = Anthology.objects.create(title='Antologia tabeli', has_illustrations=True)
        self.story = Text.objects.create(title='Tekst z tabeli', anthology=self.book, length=12,
                                         genre='Fantasy', tags='stary tag',
                                         content_warnings='Ostrzeżenia tekstu')
        self.illustration = Illustration.objects.get(text=self.story)
        self.illustration.story_url = 'https://example.org/zachowany'
        self.illustration.trigger_warnings = 'Ostrzeżenia ilustracji'
        self.illustration.coordinator_notes = 'Uwagi'
        self.illustration.illustrated_excerpt = 'Stary fragment'
        self.illustration.save()
        self.row = {'anthology': self.book.title, 'title': self.story.title, 'status': 'delivered',
                    'illustrators': ['Anna Artysta'], 'assigned_at': '2026-04-03',
                    'illustrated_excerpt': 'Nowy fragment\nDrugi akapit'}

    def run_import(self, apply=False, rows=None):
        payload = {'schema_version': 1, 'kind': 'illustration_tables', 'texts': rows or [self.row]}
        return self.run_command('import_illustration_tables', payload, apply)

    def test_preview_apply_repeat_dates_and_excluded_fields(self):
        self.row.update(genre='NIE IMPORTUJ', tags='NIE IMPORTUJ', trigger_warnings='NIE IMPORTUJ',
                        story_url='https://example.org/nie-importuj')
        self.run_import()
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.status, 'unassigned')
        self.assertEqual(self.illustration.illustrated_excerpt, 'Stary fragment')
        self.assertFalse(Illustrator.objects.exists())
        self.run_import(apply=True)
        self.illustration.refresh_from_db()
        self.story.refresh_from_db()
        self.assertEqual(self.illustration.status, "delivered")
        self.assertEqual(self.illustration.assigned_at, date(2026, 4, 3))
        self.assertEqual(self.illustration.illustrated_excerpt, self.row["illustrated_excerpt"])
        self.assertEqual(self.illustration.story_url, "https://example.org/zachowany")
        self.assertEqual(self.illustration.trigger_warnings, "Ostrzeżenia ilustracji")
        self.assertEqual(self.illustration.coordinator_notes, "Uwagi")
        self.assertEqual(
            (self.story.genre, self.story.tags, self.story.content_warnings),
            ("Fantasy", "stary tag", "Ostrzeżenia tekstu"),
        )
        repeat = self.run_import(apply=True)
        self.assertEqual(repeat["counts"], {"unchanged": 1})
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_missing_text_rolls_back_all_rows(self):
        with self.assertRaises(CommandError):
            self.run_import(apply=True, rows=[self.row, {**self.row, "title": "Nie istnieje"}])
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.status, "unassigned")
        self.assertEqual(self.illustration.illustrated_excerpt, "Stary fragment")
        self.assertFalse(Illustrator.objects.exists())
        self.assertTrue(json.loads(self.report.read_text(encoding="utf-8"))["conflicts"])

    def test_unassigned_blank_excerpt_preserves_existing_excerpt(self):
        self.run_import(apply=True, rows=[{**self.row, "status": "unassigned", "illustrators": [],
                                           "assigned_at": None, "illustrated_excerpt": ""}])
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.status, "unassigned")
        self.assertIsNone(self.illustration.assigned_at)
        self.assertEqual(self.illustration.illustrated_excerpt, "Stary fragment")
        self.assertFalse(self.illustration.illustrators.exists())

    def test_assigned_date_is_not_replaced_with_today_and_existing_contact_reused(self):
        artist = Illustrator.objects.create(first_name="Anna", last_name="Artysta", is_active=True)
        self.run_import(apply=True, rows=[{**self.row, "status": "assigned"}])
        self.illustration.refresh_from_db()
        artist.refresh_from_db()
        self.assertEqual(self.illustration.status, "assigned")
        self.assertEqual(self.illustration.assigned_at, date(2026, 4, 3))
        self.assertEqual(list(self.illustration.illustrators.all()), [artist])
        self.assertTrue(artist.is_active)
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_existing_different_artist_blocks_unassignment_and_replacement(self):
        artist = Illustrator.objects.create(first_name="Inny", last_name="Ilustrator")
        self.illustration.set_artists([artist], status="assigned")
        for row in (
            self.row,
            {**self.row, "status": "unassigned", "illustrators": [], "assigned_at": None},
        ):
            with self.assertRaises(CommandError):
                self.run_import(apply=True, rows=[row])
            self.assertEqual(list(self.illustration.illustrators.all()), [artist])
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_both_imports_reuse_pseudonym_without_new_contact(self):
        artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz',
                                            pseudonym='Graphos', email='artist@example.test')
        self.illustration.set_artists([artist], status='delivered')
        row = {'anthology': self.book.title, 'title': self.story.title, 'author': 'Autor',
               'illustrators': ['Graphos'], 'status': 'delivered', 'assigned_at': None,
               'illustrated_excerpt': ''}
        for command in ('import_illustration_credits', 'import_illustration_tables'):
            try:
                self.run_command(command, {'schema_version': 1, 'kind': 'illustration_tables',
                                           'texts': [row]}, apply=True)
            except CommandError:
                self.fail(self.report.read_text(encoding='utf-8'))
            self.assertEqual(Illustrator.objects.count(), 1)
            self.assertEqual(list(self.illustration.illustrators.all()), [artist])


class MergeIllustratorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.target = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz',
                                                preferences='Fantasy', is_active=False)
        cls.source = Illustrator.objects.create(first_name='Graphos', email='graphos@example.test',
                                                portfolio='https://example.test/portfolio',
                                                preferences='Smoki', covers=True)
        cls.other = Illustrator.objects.create(first_name='Inna osoba')
        book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.rows = []
        for number, artists in enumerate(([cls.source, cls.other], [cls.target, cls.source])):
            text = Text.objects.create(title=f'Tekst {number}', anthology=book, length=10)
            row = Illustration.objects.get(text=text)
            row.assigned_at = date(2020, 2, 3)
            row.set_artists(artists, status='delivered', preserve_assignment_date=True)
            cls.rows.append(row)

    def merge(self, apply=False):
        out = StringIO()
        call_command('merge_illustrator', first_name='Przemek', last_name='Świszcz', pseudonym='Graphos',
                     apply=apply, stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_merge_shared_assignments_and_idempotence(self):
        before = list(Illustration.objects.order_by('pk').values())
        revisions = [version_of(row) for row in self.rows]
        self.merge()
        self.target.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "")
        self.assertIsNone(self.target.email)
        self.assertEqual(self.source.email, "graphos@example.test")
        self.assertEqual([version_of(row) for row in self.rows], revisions)
        report = self.merge(apply=True)
        self.target.refresh_from_db()
        self.assertFalse(Illustrator.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.target.display_name, "Graphos")
        self.assertEqual(self.target.email, "graphos@example.test")
        self.assertEqual(self.target.preferences, "Fantasy\n\nSmoki")
        self.assertTrue(self.target.covers)
        self.assertFalse(self.target.is_active)
        self.assertEqual(
            set(self.rows[0].illustrators.values_list("pk", flat=True)),
            {self.target.pk, self.other.pk},
        )
        self.assertEqual(
            list(self.rows[1].illustrators.values_list("pk", flat=True)), [self.target.pk]
        )
        self.assertEqual(list(Illustration.objects.order_by("pk").values()), before)
        self.assertTrue(all(version_of(row) > old for row, old in zip(self.rows, revisions)))
        self.assertEqual(report["merged_id"], self.source.pk)
        repeat = self.merge(apply=True)
        self.assertIsNone(repeat["merged_id"])
        self.assertEqual(repeat["before"][0], repeat["after"])

    def test_conflicting_contact_and_ambiguous_name_abort_without_changes(self):
        self.target.email = "different@example.test"
        self.target.save()
        with self.assertRaisesMessage(CommandError, "email"):
            self.merge(apply=True)
        self.target.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "")
        self.assertTrue(self.rows[0].illustrators.filter(pk=self.source.pk).exists())
        self.target.email = None
        self.target.save()
        Illustrator.objects.create(first_name="Graphos")
        with self.assertRaisesMessage(CommandError, "kilka rekordów"):
            self.merge(apply=True)
        self.assertTrue(Illustrator.objects.filter(pk=self.source.pk).exists())
