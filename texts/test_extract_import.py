from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from authors.models import Author
from texts.models import Extract, Text, Review, Anthology
from texts.extract_data import read_markdown
from workflow.models import WorkflowStage
from core.tests import CoreTestDataMixin

HEADER = '| Imię i nazwisko | Adres e-mail | Numer telefonu | Tytuł | Data nadesłania | Nabór | Przyjęte | Odrzucone |\n|---|---|---|---|---|---|---|---|\n'
ROW = '| Anna Test | [anna@example.com](mailto\\:anna@example.com) | | Smok; Las | 2020-01-01; 2021-01-01 | Ekstrakty 1 | Smok | Las |\n'


class ExtractImportTests(CoreTestDataMixin, TestCase):
    @contextmanager
    def source(self, content=HEADER + ROW):
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'input.md'
            path.write_text(content, encoding='utf-8')
            yield path

    def run_import(self, path, **kwargs):
        output = StringIO()
        call_command('import_extracts', str(path), stdout=output, **kwargs)
        return output.getvalue()

    def test_dry_run_rolls_back_authors_and_extracts(self):
        count = Author.objects.count()
        with self.source() as path:
            self.assertIn('nic nie zapisano', self.run_import(path))
        self.assertEqual(Author.objects.count(), count)
        self.assertFalse(Extract.objects.exists())

    def test_grouped_import_case_insensitive_author_matching_and_repeat(self):
        author = Author.objects.create(first_name='Anna', last_name='Test', email='anna@example.com')
        with self.source((HEADER + ROW).replace('anna@example.com', 'ANNA@example.com')) as path:
            self.run_import(path, apply=True)
            self.assertIn('bez zmian: 1', self.run_import(path, apply=True))
        item = Extract.objects.get()
        self.assertEqual(item.author_id, author.pk)
        self.assertEqual(item.title, 'Smok\nLas')
        self.assertEqual(item.accepted_titles, 'Smok')
        self.assertEqual(item.rejected_titles, 'Las')
        self.assertEqual(item.status, 'mixed')
        self.assertEqual(item.submission_dates, '2020-01-01\n2021-01-01')
        self.assertEqual(Text.objects.count(), 0)
        self.assertEqual(Review.objects.count(), 0)
        self.assertEqual(WorkflowStage.objects.count(), 0)

    def test_same_author_separate_anthologies(self):
        with self.source(HEADER + ROW + ROW.replace('Ekstrakty 1', 'Ekstrakty 2')) as path:
            self.run_import(path, apply=True)
        self.assertEqual(Extract.objects.count(), 2)
        self.assertEqual(Extract.objects.values('author').distinct().count(), 1)

    def test_conflict_late_in_file_rolls_back_everything(self):
        invalid = ROW.replace('anna@example.com','beta@example.com').replace('| Smok | Las |','| Smok | Smok |')
        count = Author.objects.count()
        with self.source(HEADER + ROW + invalid) as path:
            with self.assertRaises(CommandError):
                self.run_import(path, apply=True)
        self.assertFalse(Extract.objects.exists())
        self.assertEqual(Author.objects.count(), count)

    def test_update_existing_requires_explicit_option(self):
        with self.source() as path:
            self.run_import(path, apply=True)
        with self.source((HEADER + ROW).replace('| Smok | Las |', '| Las | Smok |')) as path:
            with self.assertRaises(CommandError):
                self.run_import(path, apply=True)
            self.assertEqual(Extract.objects.get().accepted_titles, 'Smok')
            self.run_import(path, apply=True, update_existing=True)
            self.assertEqual(Extract.objects.get().accepted_titles, 'Las')

    def test_repeated_source_rows_are_merged(self):
        second = ROW.replace('Smok; Las','Dom').replace('| Smok | Las |','| Dom | |')
        with self.source(HEADER + ROW + second) as path:
            self.run_import(path, apply=True)
        self.assertEqual(Extract.objects.count(), 1)
        self.assertEqual(Extract.objects.get().accepted_titles, 'Smok\nDom')

    def test_forms_reject_conflicting_titles_and_duplicate_participation(self):
        self.client.force_login(self.coordinator)
        data = {'author': self.author.pk, 'title':'Smok; Las', 'submission_dates':'2020-01-01; 2021-01-01', 'recruitment':'Ekstrakty 1', 'accepted_titles':'Smok', 'rejected_titles':'Smok'}
        self.assertEqual(self.client.post(reverse('core:extract_add'), data).status_code, 400)
        data['rejected_titles'] = 'Las'
        self.assertEqual(self.client.post(reverse('core:extract_add'), data).status_code, 302)
        self.assertEqual(self.client.post(reverse('core:extract_add'), data).status_code, 400)
        response = self.client.get(reverse('core:extract_list'))
        self.assertContains(response, 'Smok<br>Las')
        self.assertContains(response, 'class="extract-group"')
        item = Extract.objects.get()
        self.assertEqual(item.status, 'mixed')

    def test_complete_attached_dataset(self):
        path = Path(__file__).resolve().parents[1] / 'data' / 'ekstrakty.md'
        if not path.exists():
            self.skipTest('Plik danych nie jest obecny w środowisku testowym')
        self.assertEqual(len(read_markdown(path.read_text())), 288)
        self.run_import(path, apply=True)
        self.assertEqual(Extract.objects.count(), 288)
        self.assertEqual(Extract.objects.values('author').distinct().count(), 258)
        for name, total in [('Ekstrakty 1',77), ('Ekstrakty 2',87), ('Ekstrakty 3',124)]:
            self.assertEqual(Extract.objects.filter(recruitment=name).count(), total)
        self.assertIn('bez zmian: 288', self.run_import(path, apply=True))
        for row in read_markdown(path.read_text()):
            item = Extract.objects.get(email=row['email'], recruitment=row['recruitment'])
            for field in ('full_name','phone_number','title','submission_dates','accepted_titles','rejected_titles'):
                self.assertEqual(getattr(item, field), row[field])
