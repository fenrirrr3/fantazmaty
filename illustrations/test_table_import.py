import json
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.management import call_command, CommandError
from django.test import TestCase
from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


class IllustrationTableImportTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.input = Path(self.tmp.name) / 'data.json'
        self.report = Path(self.tmp.name) / 'report.json'
        self.book = Anthology.objects.create(title='Antologia tabeli', has_illustrations=True)
        self.story = Text.objects.create(title='Tekst z tabeli', anthology=self.book, length=12, genre='Fantasy', tags='stary tag', content_warnings='Ostrzeżenia tekstu')
        self.illustration = Illustration.objects.get(text=self.story)
        self.illustration.story_url = 'https://example.org/zachowany'
        self.illustration.trigger_warnings = 'Ostrzeżenia ilustracji'
        self.illustration.coordinator_notes = 'Uwagi'
        self.illustration.illustrated_excerpt = 'Stary fragment'
        self.illustration.save()
        self.row = {'anthology': self.book.title, 'title': self.story.title, 'status': 'delivered', 'illustrators': ['Anna Artysta'], 'assigned_at': '2026-04-03', 'illustrated_excerpt': 'Nowy fragment\nDrugi akapit'}

    def run_import(self, apply=False, rows=None):
        self.input.write_text(json.dumps({'schema_version': 1, 'kind': 'illustration_tables', 'texts': rows or [self.row]}, ensure_ascii=False), encoding='utf-8')
        call_command('import_illustration_tables', str(self.input), apply=apply, report=str(self.report), stdout=StringIO())
        return json.loads(self.report.read_text(encoding='utf-8'))

    def test_preview_apply_repeat_dates_and_excluded_fields(self):
        self.row.update(genre='NIE IMPORTUJ', tags='NIE IMPORTUJ', trigger_warnings='NIE IMPORTUJ', story_url='https://example.org/nie-importuj')
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
        self.run_import(
            apply=True,
            rows=[
                {
                    **self.row,
                    "status": "unassigned",
                    "illustrators": [],
                    "assigned_at": None,
                    "illustrated_excerpt": "",
                }
            ],
        )
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
