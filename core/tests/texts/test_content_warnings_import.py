"""Komenda import_content_warnings: uzupełnianie ostrzeżeń, zmiana tytułu, podgląd i atomowy zapis."""
import io
import os
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from texts.models import Anthology, Text

HEADER = 'antologia\ttytuł\ttytuł w bazie\tostrzeżenia\tźródło\tostrzeżenia przed ujednoliceniem\tuzasadnienie'


def row(title, warnings, db_title='', anthology='Antologia próbna'):
    return '\t'.join((anthology, title, db_title or title, warnings, 'analiza', '', ''))


class ContentWarningsImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Antologia próbna', status=Anthology.Status.READY)
        cls.empty = Text.objects.create(title='Głos', anthology=cls.book, length=100, tags='t', genre='fantasy')
        cls.filled = Text.objects.create(title='Cisza', anthology=cls.book, length=100, tags='t', genre='fantasy',
                                         content_warnings='samobójstwo')
        cls.short = Text.objects.create(title='Diazepina', anthology=cls.book, length=100, tags='t', genre='fantasy')
        cls.novel = Anthology.objects.create(title='Powieść próbna', status=Anthology.Status.IN_PREPARATION, is_novel=True)

    def run_import(self, *rows, apply=False, args=(), header=HEADER):
        handle, path = tempfile.mkstemp(suffix='.tsv')
        with os.fdopen(handle, 'w', encoding='utf-8') as file:
            file.write('\n'.join((header, *rows)))
        self.addCleanup(os.remove, path)
        out = io.StringIO()
        try:
            call_command('import_content_warnings', path, *args, *(['--apply'] if apply else []), stdout=out)
        except CommandError as error:
            return out.getvalue(), str(error)
        return out.getvalue(), None

    def test_import_cases(self):
        """Puste pole uzupełniane, inne istniejące zgłaszane (albo zastępowane z --overwrite), tytuł z pliku
        zastępuje tytuł z bazy, puste ostrzeżenia pomijane, dopasowanie bez względu na wielkość liter."""
        cases = (
            ('uzupełnienie', [row('głos', 'przemoc fizyczna')], (), {'empty': 'przemoc fizyczna'}, 'uzupełnione ostrzeżenia: 1'),
            ('rozbieżność', [row('Cisza', 'tortury')], (), {'filled': 'samobójstwo'}, 'CMS: samobójstwo | plik: tortury'),
            ('nadpisanie', [row('Cisza', 'tortury')], ('--overwrite',), {'filled': 'tortury'}, 'zastąpione ostrzeżenia: 1'),
            ('puste', [row('Głos', '')], (), {'empty': ''}, 'wiersze bez ostrzeżeń: 1'),
        )
        for label, rows, args, expected, message in cases:
            with self.subTest(label):
                out, error = self.run_import(*rows, apply=True, args=args)
                self.assertIsNone(error, out)
                self.assertIn(message, out)
                for attr, value in expected.items():
                    self.assertEqual(Text.objects.get(pk=getattr(self, attr).pk).content_warnings, value)
                Text.objects.filter(pk=self.empty.pk).update(content_warnings='')
                Text.objects.filter(pk=self.filled.pk).update(content_warnings='samobójstwo')

    def test_title_from_file_replaces_database_title(self):
        """Tekst znaleziony po „tytule w bazie” dostaje tytuł z pliku (wersja z antologii)."""
        out, error = self.run_import(row('2-metylo-4-(4-metylopiperazyn-1-ylo)-10H-benzo[b]thieno[2,3-e][1,4]diazepina', 'uzależnienia', db_title='Diazepina'), apply=True)
        self.assertIsNone(error, out)
        self.short.refresh_from_db()
        self.assertEqual((self.short.title, self.short.content_warnings), ('2-metylo-4-(4-metylopiperazyn-1-ylo)-10H-benzo[b]thieno[2,3-e][1,4]diazepina', 'uzależnienia'))

    def test_preview_saves_nothing(self):
        """Bez --apply nic nie jest zapisywane."""
        out, error = self.run_import(row('Głos', 'przemoc fizyczna'))
        self.assertIsNone(error)
        self.assertIn('Podgląd – nic nie zapisano', out)
        self.assertEqual(Text.objects.get(pk=self.empty.pk).content_warnings, '')

    def test_errors_abort_whole_import(self):
        """Błędny wiersz (brak antologii, tekstu, powieść, powtórzony tekst) – nic nie zapisane."""
        cases = (
            ('brak antologii', row('Głos', 'x', anthology='Nieznana'), 'nie ma antologii'),
            ('brak tekstu', row('Nieznany', 'x'), 'nie ma tekstu'),
            ('powieść', row('Rozdział', 'x', anthology='Powieść próbna'), 'to powieść'),
            ('powtórzenie', row('Cisza', 'x'), 'więcej niż raz'),
        )
        for label, bad, message in cases:
            with self.subTest(label):
                rows = [row('Głos', 'przemoc fizyczna'), bad] + ([row('Cisza', 'y')] if label == 'powtórzenie' else [])
                out, error = self.run_import(*rows, apply=True)
                self.assertIsNotNone(error)
                self.assertIn(message, out)
                self.assertEqual(Text.objects.get(pk=self.empty.pk).content_warnings, '')

    def test_wrong_header_is_rejected(self):
        """Nagłówek bez wymaganych kolumn kończy się błędem komendy."""
        _out, error = self.run_import(row('Głos', 'x'), header='tytuł\tostrzeżenia')
        self.assertIn('Nieprawidłowy nagłówek', error)
