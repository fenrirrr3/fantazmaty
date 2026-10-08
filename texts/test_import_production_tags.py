import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse

from texts.models import Anthology, Text, VocabularyTerm
from texts.management.commands.import_production_tags import SCHEMA
from workflow.models import WorkflowStage
from illustrations.models import Illustration


class ProductionTagsImportTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / 'input.json'
        self.book = Anthology.objects.create(title='Ballady ze spalonego traktu', has_illustrations=True)
        self.story = Text.objects.create(anthology=self.book, title='Dzięciecy hejnał', length=111,
                                         tags='stare', genre='fantasy', content_warnings='zostaw')
        Illustration.objects.get_or_create(text=self.story)
        self.other = Text.objects.create(anthology=self.book, title='Inny', length=222, tags='bez zmian')
        self.rows = [{'anthology': self.book.title, 'title': 'Dziecięcy hejnał',
                      'title_aliases': ['Dzięciecy hejnał'], 'tags': 'muzyka, dziecko',
                      'genre': 'fantasy humorystyczne'}]
        self.counter = 0

    def run_import(self, apply=False):
        self.input.write_text(json.dumps({'schema': SCHEMA, 'count': len(self.rows), 'texts': self.rows}, ensure_ascii=False), encoding='utf-8')
        self.counter += 1
        report = self.root / f'report-{self.counter}.json'
        call_command('import_production_tags', str(self.input), apply=apply, report=str(report), stdout=io.StringIO())
        return json.loads(report.read_text(encoding='utf-8'))

    def test_preview_apply_idempotence_and_public_projection(self):
        before = Text.objects.filter(pk=self.story.pk).values().get()
        stages = list(WorkflowStage.objects.filter(text=self.story).values())
        terms = VocabularyTerm.objects.count()
        preview = self.run_import()
        self.assertEqual(preview['counts'], {'planned': 1, 'updated': 0, 'unchanged': 0})
        self.assertTrue(preview['texts'][0]['matched_alias'])
        self.assertEqual(Text.objects.filter(pk=self.story.pk).values().get(), before)
        self.assertEqual(VocabularyTerm.objects.count(), terms)
        applied = self.run_import(True)
        self.assertEqual(applied['texts'][0]['before'], {'tags': 'stare', 'genre': 'fantasy'})
        after = Text.objects.filter(pk=self.story.pk).values().get()
        self.assertEqual({k for k in before if before[k] != after[k]}, {'tags', 'genre'})
        self.assertEqual(list(WorkflowStage.objects.filter(text=self.story).values()), stages)
        self.other.refresh_from_db()
        self.assertEqual(self.other.tags, 'bez zmian')
        self.assertEqual(self.run_import(True)['counts']['planned'], 0)
        page = self.client.get(reverse('illustrations:external_illustrations'))
        self.assertContains(page, 'muzyka')
        self.assertContains(page, 'fantasy humorystyczne')

    def test_missing_text_prevents_all_changes(self):
        self.rows.append({**self.rows[0], 'title': 'Nie istnieje', 'title_aliases': []})
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.story.refresh_from_db()
        self.assertEqual(self.story.tags, 'stare')
        report = json.loads((self.root / 'report-1.json').read_text(encoding='utf-8'))
        self.assertEqual(report['mode'], 'wycofano – nic nie zapisano')
        self.assertEqual(report['counts']['updated'], 0)

    def test_alias_ambiguity_and_overlapping_input_abort(self):
        Text.objects.create(anthology=self.book, title='Dziecięcy hejnał', length=1)
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.rows.append(dict(self.rows[0]))
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.story.refresh_from_db()
        self.assertEqual(self.story.tags, 'stare')

    def test_duplicate_anthology_aborts(self):
        Anthology.objects.create(title=self.book.title)
        with self.assertRaises(CommandError):
            self.run_import(True)

    def test_no_fuzzy_matching_but_unicode_case_and_whitespace_allowed(self):
        self.rows[0]['title_aliases'] = ['  DZIĘCIECY   HEJNAŁ ']
        self.assertEqual(self.run_import()['counts']['planned'], 1)
        self.rows[0]['title_aliases'] = []
        with self.assertRaises(CommandError):
            self.run_import(True)

    def test_dictionary_aliases_and_field_limits(self):
        target = VocabularyTerm.objects.create(kind='tag', name='muzyka')
        VocabularyTerm.objects.create(kind='tag', name='muzyczne', canonical=target)
        self.rows[0]['tags'] = 'muzyczne, muzyka, dziecko'
        report = self.run_import(True)
        self.assertEqual(report['texts'][0]['after']['tags'], 'muzyka, dziecko')
        self.rows[0]['genre'] = 'x' * 101
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.story.refresh_from_db()
        self.assertEqual(self.story.genre, 'fantasy humorystyczne')

    def test_database_failure_rolls_back_text_and_dictionary(self):
        self.rows[0]['tags'] = 'zupełnie nowe hasło'
        before = VocabularyTerm.objects.count()
        with patch('texts.management.commands.import_production_tags.write_report', side_effect=[None, OSError('test'), None]):
            with self.assertRaises(CommandError):
                self.run_import(True)
        self.story.refresh_from_db()
        self.assertEqual(self.story.tags, 'stare')
        self.assertEqual(VocabularyTerm.objects.count(), before)

    def test_existing_report_is_not_overwritten(self):
        report = self.root / 'existing.json'
        report.write_text('poprzedni raport', encoding='utf-8')
        with self.assertRaises(CommandError):
            call_command('import_production_tags', str(self.input), apply=True, report=str(report))
        self.assertEqual(report.read_text(encoding='utf-8'), 'poprzedni raport')
        self.story.refresh_from_db()
        self.assertEqual(self.story.tags, 'stare')
