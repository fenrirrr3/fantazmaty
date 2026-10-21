"""Komenda import_audiobook_publications: nowe audiobooki z pliku TSV, uzupełnianie istniejących, podgląd i atomowy zapis."""
import io
import os
import tempfile
from datetime import date

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from authors.models import Author
from core.models import AudioContributor, Audiobook, AudiobookStage, DiscordNotification, NotificationRule, WorkflowEvent
from fantazmaty.testing import create_superuser
from texts.models import Anthology, Text

HEADER = 'antologia\tautor\ttytuł\tdata premiery\tyoutube\thearthis\tkolejne części\tlektor\tuwagi'
YT = 'https://www.youtube.com/watch?v=abc123'
YT2 = 'https://youtu.be/def456'
HT = 'https://hearthis.at/testowe/01-glos/'
HT2 = 'https://hearthis.at/testowe/01-glos-cz2/'


def row(title='Głos', anthology='Antologia próbna', author='', premiere='01.02.2021', youtube=YT, hearthis=HT,
        parts='', narrator='Jan Lektor', notes=''):
    return '\t'.join((anthology, author, title, premiere, youtube, hearthis, parts, narrator, notes))


class AudiobookPublicationImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('audio-import')
        cls.book = Anthology.objects.create(title='Antologia próbna', status=Anthology.Status.READY)
        cls.text = Text.objects.create(title='Głos', anthology=cls.book, length=100, tags='t', genre='fantasy')
        cls.text.authors.add(Author.objects.create(first_name='Anna', last_name='Testowa'))
        cls.quoted = Text.objects.create(title='„Cisza” i ’echo’', anthology=cls.book, length=100, tags='t', genre='fantasy')
        for name, pseudonym in (('Piotr', 'Pierwszy'), ('Paweł', 'Drugi')):
            text = Text.objects.create(title='Bliźniak', anthology=cls.book, length=100, tags='t', genre='fantasy')
            text.authors.add(Author.objects.create(first_name=name, last_name='Dubel', pseudonym=pseudonym))
        cls.narrator = AudioContributor.objects.create(name='Jan Lektor')

    def run_import(self, *rows, apply=False, args=(), header=HEADER):
        content = '\n'.join((header, *rows)).encode()
        handle, path = tempfile.mkstemp(suffix='.tsv')
        with os.fdopen(handle, 'wb') as file:
            file.write(content)
        self.addCleanup(os.remove, path)
        out = io.StringIO()
        options = [*args] + (['--apply'] if apply else [])
        try:
            call_command('import_audiobook_publications', path, *options, stdout=out)
        except CommandError as error:
            return out.getvalue(), str(error)
        return out.getvalue(), None

    def test_new_audiobook_is_published_without_stages_and_without_discord(self):
        """Nowy audiobook: status Opublikowane, data, linki, kolejne części, lektor ze spisu (bez nowego wpisu),
        brak etapów, wpis w dzienniku bez powiadomienia mimo włączonej reguły „Produkcja audiobooka”."""
        rule = NotificationRule.objects.get(trigger='audiobook_production')
        rule.enabled, rule.channel = True, 'Audiobooki'
        rule.save()
        out, error = self.run_import(row(parts=f'{YT2} | {HT2}'), apply=True)
        self.assertIsNone(error, out)
        self.assertIn('Zapisano.', out)
        self.assertIn('nowe audiobooki: 1', out)
        audio = Audiobook.objects.get(text=self.text)
        self.assertEqual((audio.status, audio.premiere_date, audio.youtube_url, audio.hearthis_url, audio.active_stage_id,
                          audio.narrator_contact_id, audio.narrator_name),
                         ('published', date(2021, 2, 1), YT, HT, None, self.narrator.pk, 'Jan Lektor'))
        self.assertEqual(audio.additional_links, [{'service': 'youtube', 'part': 2, 'url': YT2},
                                                  {'service': 'hearthis', 'part': 2, 'url': HT2}])
        self.assertFalse(AudiobookStage.objects.filter(text=self.text).exists())
        self.assertEqual(AudioContributor.objects.count(), 1)
        event = WorkflowEvent.objects.get(kind='audiobook', text=self.text)
        self.assertEqual((event.actor_id, event.previous_status, event.next_status),
                         (self.admin.pk, 'Audiobook: –', 'Audiobook: Opublikowane'))
        self.assertIn('Import publikacji', event.details)
        self.assertFalse(DiscordNotification.objects.exists())

    def test_existing_audiobook_fills_empty_fields_and_reports_differences(self):
        """Istniejący audiobook: puste pola uzupełniane, wypełnione nie są zmieniane (różnica w raporcie), status
        i etapy nienaruszone, kolejne części dopisywane bez powtórzeń, --overwrite-premiere zastępuje datę."""
        other = AudioContributor.objects.create(name='Inna Osoba')
        cases = (
            ('uzupełnienie pustych pól', dict(status='editing'), row(parts=f'{YT2} | {HT2}'), (),
             dict(premiere_date=date(2021, 2, 1), youtube_url=YT, hearthis_url=HT, narrator_contact_id=self.narrator.pk,
                  status='editing', additional_links=[{'service': 'youtube', 'part': 2, 'url': YT2},
                                                      {'service': 'hearthis', 'part': 2, 'url': HT2}]),
             ('uzupełnione daty premiery: 1', 'uzupełnione linki: 2', 'dopisane kolejne części: 2',
              'uzupełnieni lektorzy: 1', 'status audiobooka w CMS: Montaż')),
            ('wypełnione pola bez zmian', dict(status='published', premiere_date=date(2020, 1, 1), youtube_url=YT2, hearthis_url=HT2,
                                                narrator_contact=other, additional_links=[{'service': 'youtube', 'part': 2, 'url': YT}]),
             row(parts=f'{YT} | {HT2}'), (),
             dict(premiere_date=date(2020, 1, 1), youtube_url=YT2, hearthis_url=HT2, narrator_contact_id=other.pk,
                  additional_links=[{'service': 'youtube', 'part': 2, 'url': YT}]),
             ('różna data premiery: CMS 01.01.2020 / plik 01.02.2021', f'różny link YouTube: CMS {YT2} / plik {YT}',
              f'różny link HearThis: CMS {HT2} / plik {HT}', 'inny lektor: CMS Inna Osoba / plik Jan Lektor',
              'bez zmian: 1', 'Rozbieżności (4)')),
            ('--overwrite-premiere', dict(premiere_date=date(2020, 1, 1), youtube_url=YT, hearthis_url=HT,
                                           narrator_contact=self.narrator),
             row(), ('--overwrite-premiere',), dict(premiere_date=date(2021, 2, 1)),
             ('uzupełnione daty premiery: 1', 'data premiery 01.01.2020 → 01.02.2021')),
        )
        for label, initial, line, args, expected, messages in cases:
            with self.subTest(label):
                Audiobook.objects.filter(text=self.text).delete()
                audio = Audiobook.objects.create(text=self.text, **initial)
                out, error = self.run_import(line, apply=True, args=args)
                self.assertIsNone(error, out)
                audio.refresh_from_db()
                self.assertEqual({key: getattr(audio, key) for key in expected}, expected)
                self.assertFalse(AudiobookStage.objects.filter(text=self.text).exists())
                for message in messages:
                    self.assertIn(message, out)

    def test_dry_run_saves_nothing(self):
        """Bez --apply komenda pokazuje plan, ale nie zapisuje audiobooka, wpisu w spisie ani dziennika."""
        out, error = self.run_import(row(narrator='Nowa Lektorka'))
        self.assertIsNone(error, out)
        self.assertIn('Podgląd – nic nie zapisano. Użyj --apply.', out)
        self.assertIn('nowe audiobooki: 1', out)
        self.assertIn('nowe wpisy w spisie lektorów: 1', out)
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudioContributor.objects.filter(name='Nowa Lektorka').exists())
        self.assertFalse(WorkflowEvent.objects.exists())

    def test_row_errors_block_the_whole_import(self):
        """Błędny wiersz (brak antologii, brak tekstu, zły adres, zła data, zła kolejna część, za mało kolumn)
        daje kod błędu i nic nie zapisuje – także poprawnego wiersza z tego samego pliku."""
        cases = (
            ('brak antologii', row(anthology='Nie ma'), 'nie ma antologii „Nie ma”'),
            ('brak tekstu', row(title='Nie ma'), 'nie ma tekstu „Nie ma”'),
            ('zły adres', row(youtube='https://example.com/x'), 'zapis odrzucony: Wklej prawidłowy link do YouTube.'),
            ('zła data', row(premiere='2021-02-01'), 'nieprawidłowa data premiery „2021-02-01”'),
            ('zła kolejna część', row(parts='https://example.com/x'), 'nieprawidłowy adres kolejnej części'),
            ('za mało kolumn', 'Antologia próbna\tGłos', 'oczekiwano 9 kolumn, jest 2'),
        )
        for label, bad, expected in cases:
            with self.subTest(label):
                out, error = self.run_import(bad, row(title='„Cisza” i ’echo’'), apply=True)
                self.assertIn('Błędne wiersze: 1', error)
                self.assertIn(expected, out)
                self.assertIn('wiersz 2:', out)
                self.assertFalse(Audiobook.objects.exists())
                self.assertFalse(WorkflowEvent.objects.exists())
        out, error = self.run_import(row(), apply=True, header='antologia\ttytuł')
        self.assertIn('Nieprawidłowy nagłówek', error)
        self.assertFalse(Audiobook.objects.exists())

    def test_title_matching_ignores_quotes_case_and_resolves_duplicates_by_author(self):
        """Tytuł dopasowany mimo innych cudzysłowów i wielkości liter; dwa teksty o tym samym tytule rozstrzyga
        kolumna autora (pseudonim albo imię i nazwisko), a bez pasującego autora wiersz jest błędny."""
        cases = (
            ('inne cudzysłowy', row(title='"cisza" i \'ECHO\''), None, self.quoted),
            ('duplikat – pseudonim', row(title='Bliźniak', author='Drugi'), None, Text.objects.get(authors__pseudonym='Drugi')),
            ('duplikat – imię i nazwisko', row(title='bliźniak', author='piotr dubel'), None,
             Text.objects.get(authors__pseudonym='Pierwszy')),
            ('duplikat – bez autora', row(title='Bliźniak'), 'kilka tekstów „Bliźniak”', None),
            ('duplikat – obcy autor', row(title='Bliźniak', author='Ktoś Inny'), 'kilka tekstów „Bliźniak”', None),
        )
        for label, line, expected_error, text in cases:
            with self.subTest(label):
                out, error = self.run_import(line, apply=True)
                if expected_error:
                    self.assertIn('Błędne wiersze: 1', error)
                    self.assertIn(expected_error, out)
                    self.assertFalse(Audiobook.objects.exists())
                else:
                    self.assertIsNone(error, out)
                    self.assertEqual(list(Audiobook.objects.values_list('text_id', flat=True)), [text.pk])
                    Audiobook.objects.all().delete()

    def test_user_option_and_missing_superuser(self):
        """--user wskazuje konto dziennika (e-mail), nieistniejący adres to błąd, a bez superusera i --user import
        się nie uruchamia."""
        out, error = self.run_import(row(), apply=True, args=('--user', 'audio-import@example.test'))
        self.assertIsNone(error, out)
        self.assertEqual(WorkflowEvent.objects.get().actor_id, self.admin.pk)
        Audiobook.objects.all().delete()
        out, error = self.run_import(row(), apply=True, args=('--user', 'nikt@example.test'))
        self.assertIn('Nie ma konta z adresem „nikt@example.test”', error)
        self.admin.delete()
        out, error = self.run_import(row(), apply=True)
        self.assertIn('Brak konta superusera', error)
        self.assertFalse(Audiobook.objects.exists())
