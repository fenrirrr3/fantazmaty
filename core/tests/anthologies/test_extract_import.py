"""Komenda import_extracts: parsowanie pliku TSV, dopasowanie autorów, dopisywanie tytułów, podgląd i atomowy zapis."""
import io
import os
import tempfile
from datetime import date

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from authors.models import Author, BlacklistEntry
from core.edit_versions import version_of
from texts.models import Extract

HEADER = 'imię i nazwisko\tpseudonim\te-mail\ttytuły (rozdzielone |)\tdecyzja'
RECRUITMENT = 'Ekstrakty Próbne'


class ExtractImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(first_name='Anna', last_name='Zięba', email='anna@example.test')
        cls.entry = Extract.objects.create(
            author=cls.author, full_name='Anna Zięba', email='anna@example.test', recruitment=RECRUITMENT,
            title='Mgła nad rzeką\nCisza, potem burza\nBez decyzji', accepted_titles='Mgła nad rzeką',
            rejected_titles='Cisza, potem burza', submission_dates='2026-01-05',
        )

    def run_import(self, *rows, apply=False, header=HEADER, raw=None, args=()):
        content = raw if raw is not None else '\n'.join((header, *rows)).encode()
        handle, path = tempfile.mkstemp(suffix='.tsv')
        with os.fdopen(handle, 'wb') as file:
            file.write(content)
        self.addCleanup(os.remove, path)
        out = io.StringIO()
        options = ['--recruitment', RECRUITMENT, *args] + (['--apply'] if apply else [])
        try:
            call_command('import_extracts', path, *options, stdout=out)
        except CommandError as error:
            return out.getvalue(), str(error)
        return out.getvalue(), None

    def test_parsing_accepts_bom_crlf_and_blank_lines_and_reports_bad_rows(self):
        """BOM, CRLF i puste wiersze są poprawne; brak kolumny, zła decyzja, e-mail lub nagłówek to błąd."""
        good = 'Jan Nowak\t\tjan@example.test\tPierwszy, drugi|Trzeci\tPRZYJĘTE'
        cases = (
            ('BOM i CRLF', ('﻿' + HEADER + '\r\n\r\n' + good + '\r\n\r\n').encode(), None, '2 tytuły'),
            ('brak kolumny', None, 'Jan Nowak\tjan@example.test\tTytuł\tprzyjęte', 'oczekiwano 5 kolumn, jest 4'),
            ('zła decyzja', None, 'Jan Nowak\t\tjan@example.test\tTytuł\tmoże', 'nieznana decyzja „może”'),
            ('zły e-mail', None, 'Jan Nowak\t\tjan@example\tTytuł\todrzucone', 'nieprawidłowy adres e-mail'),
            ('jedno słowo', None, 'Nowak\t\tjan@example.test\tTytuł\todrzucone', 'podaj imię i nazwisko'),
            ('średnik w tytule', None, 'Jan Nowak\t\tjan@example.test\tA; B\todrzucone', 'zawiera średnik'),
        )
        for label, raw, row, expected in cases:
            with self.subTest(label):
                out, error = self.run_import(row, raw=raw)
                if row is None:
                    self.assertIsNone(error)
                    self.assertIn('przyjęte: „Pierwszy, drugi”, „Trzeci”', out)
                else:
                    self.assertIn('Błędne wiersze: 1', error)
                    self.assertIn(expected, out)
        out, error = self.run_import(header='imię\te-mail')
        self.assertIn('Nieprawidłowy nagłówek', error)

    def test_author_matching_and_new_author_with_pseudonym(self):
        """Autor dopasowany po e-mailu (bez wielkości liter) lub po imieniu i nazwisku; inaczej nowy autor z pseudonimem."""
        Author.objects.create(first_name='Ewa', last_name='Łoś', email='ewa@example.test', pseudonym='Sowa')
        Author.objects.create(first_name='Piotr', last_name='Kos', email='p1@example.test')
        Author.objects.create(first_name='Piotr', last_name='Kos', email='p2@example.test')
        cases = (
            ('po e-mailu', 'Ktoś Inny\t\tANNA@Example.test\tNowy\tprzyjęte', f'istniejący autor #{self.author.pk} Anna Zięba (dopasowany po e-mailu)'),
            ('po imieniu i nazwisku', 'ewa   ŁOŚ\tSowa\tewa.nowy@example.test\tNowy\tprzyjęte', '(dopasowany po imieniu i nazwisku)'),
            ('niejednoznaczne', 'Piotr Kos\t\tp3@example.test\tNowy\tprzyjęte', 'kilku autorów o tym imieniu i nazwisku'),
            ('nowy autor', 'jan maria rokita\tJMR\tjmr@example.test\tNowy\tprzyjęte', 'nowy autor: imię „Jan”, nazwisko „Maria Rokita”, pseudonim „JMR”'),
        )
        for label, row, expected in cases:
            with self.subTest(label):
                out, _ = self.run_import(row)
                self.assertIn(expected, out)
        self.assertIn('imię i nazwisko w pliku („Ktoś Inny”) różni się', self.run_import(cases[0][1])[0])
        self.assertIn('e-mail w pliku (ewa.nowy@example.test) różni się', self.run_import(cases[1][1])[0])
        out, error = self.run_import(cases[3][1], apply=True)
        self.assertIsNone(error)
        author = Author.objects.get(email='jmr@example.test')
        self.assertEqual((author.first_name, author.last_name, author.pseudonym), ('Jan', 'Maria Rokita', 'JMR'))
        entry = Extract.objects.get(author=author)
        self.assertEqual((entry.full_name, entry.title, entry.accepted_titles, entry.status),
                         ('Jan Maria Rokita', 'Nowy', 'Nowy', Extract.Status.ACCEPTED))

    def test_existing_author_keeps_data_and_warns_about_pseudonym(self):
        """Różny pseudonim i e-mail istniejącego autora dają ostrzeżenie, a dane autora pozostają bez zmian."""
        out, error = self.run_import('Anna Zięba\tZiębka\tinny@example.test\tNowy\tprzyjęte', apply=True)
        self.assertIsNone(error)
        self.assertIn('pseudonim w pliku („Ziębka”) różni się', out)
        self.author.refresh_from_db()
        self.assertEqual((self.author.email, self.author.pseudonym), ('anna@example.test', ''))

    def test_appends_titles_to_existing_entry_and_skips_present_ones(self):
        """Nowe tytuły trafiają do nadesłanych i do listy decyzji, obecne są pomijane, a wersja wpisu rośnie."""
        version = version_of(self.entry)
        out, error = self.run_import(
            'Anna Zięba\t\tanna@example.test\tmgła  nad rzeką|Bez decyzji|Nowy tekst\tprzyjęte',
            'Anna Zięba\t\tanna@example.test\tOdrzucony nowy\todrzucone', apply=True)
        self.assertIsNone(error)
        self.assertIn('dopisano tytuły (przyjęte): „Bez decyzji”, „Nowy tekst”', out)
        self.assertIn('pominięto (już są): „mgła nad rzeką”', out)
        self.assertIn('dopisane tytuły: 3, pominięte tytuły (już są): 1', out)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.title.splitlines(),
                         ['Mgła nad rzeką', 'Cisza, potem burza', 'Bez decyzji', 'Nowy tekst', 'Odrzucony nowy'])
        self.assertEqual(self.entry.accepted_titles.splitlines(), ['Mgła nad rzeką', 'Bez decyzji', 'Nowy tekst'])
        self.assertEqual(self.entry.rejected_titles.splitlines(), ['Cisza, potem burza', 'Odrzucony nowy'])
        self.assertEqual((self.entry.status, self.entry.submission_dates), (Extract.Status.MIXED, '2026-01-05'))
        self.assertGreater(version_of(self.entry), version)

    def test_row_errors_leave_database_unchanged(self):
        """Konflikt decyzji lub czarna lista to błąd wiersza; z --apply nic nie jest zapisane, także poprawne wiersze."""
        Author.objects.create(first_name='Zła', last_name='Osoba', email='zla@example.test', is_blacklisted=True)
        BlacklistEntry.objects.create(email='lista@example.test')
        valid = 'Nowa Osoba\t\tnowa@example.test\tDobry\tprzyjęte'
        cases = (
            ('konflikt decyzji', 'Anna Zięba\t\tanna@example.test\tCisza,  potem burza\tprzyjęte', 'mają w CMS przeciwną decyzję: „Cisza, potem burza”'),
            ('autor na czarnej liście', 'Zła Osoba\t\tinna@example.test\tTytuł\tprzyjęte', 'jest na czarnej liście'),
            ('e-mail na czarnej liście', 'Ktoś Nowy\t\tLISTA@example.test\tTytuł\tprzyjęte', 'adres e-mail jest na czarnej liście'),
        )
        for label, row, expected in cases:
            with self.subTest(label):
                counts = (Author.objects.count(), Extract.objects.count())
                out, error = self.run_import(valid, row, apply=True)
                self.assertIn(expected, out)
                self.assertIn('Błędne wiersze: 1. Nic nie zapisano', error)
                self.assertIn('nowi autorzy: 1, nowe wpisy Ekstraktów: 1', error)
                self.assertEqual((Author.objects.count(), Extract.objects.count()), counts)

    def test_dry_run_simulates_rows_in_order_without_saving(self):
        """Podgląd nic nie zapisuje, ale kolejny wiersz tego samego nowego autora trafia do wpisu z poprzedniego."""
        version = version_of(self.entry)
        out, error = self.run_import('Jan Nowak\t\tjan@example.test\tPierwszy\tprzyjęte',
                                     'Jan Nowak\t\tjan@example.test\tDrugi\todrzucone',
                                     'Anna Zięba\t\tanna@example.test\tNowy\tprzyjęte')
        self.assertIsNone(error)
        self.assertEqual(out.count('nowy autor'), 1)
        self.assertIn('dopisano tytuły (odrzucone): „Drugi”', out)
        self.assertIn('Podgląd – nic nie zapisano', out)
        self.assertEqual((Author.objects.count(), Extract.objects.count()), (1, 1))
        self.assertEqual(Extract.objects.get().title, self.entry.title)
        self.assertEqual(version_of(self.entry), version)

    def test_recruitment_name_and_submission_date(self):
        """Pisownia naboru z bazy ma pierwszeństwo, nowy nabór daje ostrzeżenie, a --date ustala datę nadesłania."""
        row = 'Jan Nowak\t\tjan@example.test\tTytuł\tprzyjęte'
        cases = (
            ('istniejąca pisownia', ['--recruitment', 'ekstrakty   próbne'], RECRUITMENT, timezone.localdate(), 'Użyto istniejącej pisowni naboru'),
            ('nowy nabór i --date', ['--recruitment', 'Ekstrakty Nowe', '--date', '2026-03-04'], 'Ekstrakty Nowe', date(2026, 3, 4), 'nie ma jeszcze naboru „Ekstrakty Nowe”'),
        )
        for label, args, recruitment, submitted, expected in cases:
            with self.subTest(label):
                out, error = self.run_import(row, apply=True, args=args)
                self.assertIsNone(error)
                self.assertIn(expected, out)
                entry = Extract.objects.get(author__email='jan@example.test', recruitment=recruitment)
                self.assertEqual(entry.submitted_at, submitted)
                entry.delete()
        out, error = self.run_import(row, args=['--date', '4.03.2026'])
        self.assertIn('RRRR-MM-DD', error)
