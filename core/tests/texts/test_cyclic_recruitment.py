"""Nabory cykliczne (Fantazmaty, nanoFantazje): rozpoznawanie tomu, odstęp od poprzedniego tekstu autora,
ostrzeżenia przy zapisie zgłoszenia (pojedynczo, importem i ze skrzynki) oraz ustawienia na stronie „Dodaj do recenzji”."""
import datetime
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.cyclic_recruitment import (
    CyclicRecruitment, interval_unit_label, recent_submission_warning, recruitment_for)
from core.forms import ReviewBulkImportForm
from core.models import MailboxConnection
from core.services.mailbox_import import parse_message, prepare_forms
from core.tests.recruitment.support import named_mail
from core.tests.recruitment.test_review_intake import IntakePanels
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Review
from texts.services import get_review_submission_warnings

TODAY = datetime.date(2026, 10, 29)


def recruitment(name='Fantazmaty', value=3, unit='months'):
    return CyclicRecruitment(name=name, interval_value=value, interval_unit=unit)


class RecruitmentRulesTests(SimpleTestCase):
    """Czyste reguły: dopasowanie tytułu tomu, początek okna odstępu i polska odmiana jednostek."""

    def test_volume_title_matches_series_regardless_of_number(self):
        series = [recruitment('Fantazmaty'), recruitment('nanoFantazje')]
        for title, expected in {
            'Fantazmaty 6': 'Fantazmaty', 'Fantazmaty': 'Fantazmaty', 'fantazmaty 12': 'Fantazmaty',
            'Fantazmaty6': 'Fantazmaty', '  Fantazmaty 2 ': 'Fantazmaty', 'Fantazmaty: Groza': 'Fantazmaty',
            'Fantazmaty – tom 3': 'Fantazmaty', 'nanoFantazje 1.0': 'nanoFantazje', 'NANOFANTAZJE 3': 'nanoFantazje',
            'Fantazmatyczne opowieści': None, 'Nowe Fantazmaty 6': None, 'nanoFantazjeX': None, '': None,
            'Na pokład, psubraty': None,
        }.items():
            with self.subTest(title):
                found = recruitment_for(title, series)
                self.assertEqual(found.name if found else None, expected)

    def test_longest_name_wins(self):
        series = [recruitment('Fantazje'), recruitment('Fantazje Extra')]
        self.assertEqual(recruitment_for('Fantazje Extra 2', series).name, 'Fantazje Extra')
        self.assertEqual(recruitment_for('Fantazje 2', series).name, 'Fantazje')

    def test_window_start(self):
        for (today, value, unit), expected in {
            (TODAY, 3, 'months'): datetime.date(2026, 7, 29),
            (datetime.date(2026, 5, 31), 3, 'months'): datetime.date(2026, 2, 28),
            (datetime.date(2028, 5, 31), 3, 'months'): datetime.date(2028, 2, 29),
            (datetime.date(2026, 1, 15), 1, 'months'): datetime.date(2025, 12, 15),
            (datetime.date(2026, 2, 10), 14, 'months'): datetime.date(2024, 12, 10),
            (datetime.date(2026, 3, 31), 12, 'months'): datetime.date(2025, 3, 31),
            (TODAY, 2, 'weeks'): datetime.date(2026, 10, 15),
            (datetime.date(2026, 1, 3), 1, 'weeks'): datetime.date(2025, 12, 27),
        }.items():
            with self.subTest(today=today, value=value, unit=unit):
                self.assertEqual(recruitment(value=value, unit=unit).window_start(today), expected)

    def test_interval_labels(self):
        for (value, unit), expected in {
            (1, 'weeks'): 'tydzień', (2, 'weeks'): 'tygodnie', (5, 'weeks'): 'tygodni', (22, 'weeks'): 'tygodnie',
            (12, 'weeks'): 'tygodni', (1, 'months'): 'miesiąc', (3, 'months'): 'miesiące', (6, 'months'): 'miesięcy',
        }.items():
            with self.subTest(value=value, unit=unit):
                self.assertEqual(interval_unit_label(value, unit), expected)
        self.assertEqual(recruitment(value=3).interval_label, '3 miesiące')
        self.assertEqual(recruitment(value=None).interval_label, 'bez sprawdzania')

    def test_recent_submission_warning_text(self):
        book = Anthology(pk=5, title='Fantazmaty 5')
        previous = Review(pk=41, title='Smoki', anthology=book, created_at=datetime.date(2026, 10, 8))
        for created, elapsed in {datetime.date(2026, 10, 8): '21 dni temu', TODAY: 'dzisiaj',
                                 datetime.date(2026, 10, 28): '1 dzień temu'}.items():
            with self.subTest(created):
                previous.created_at = created
                self.assertEqual(
                    recent_submission_warning(recruitment(), [previous], today=TODAY),
                    f'Nabór cykliczny Fantazmaty: autor przysłał poprzedni tekst {elapsed} ({created:%d.%m.%Y}, '
                    '#41 „Smoki”, Fantazmaty 5), a minimalny odstęp to 3 miesiące. Sprawdź przed dodaniem.')

    def test_no_warning_outside_window_other_series_or_without_interval(self):
        own = Review(pk=1, title='A', anthology=Anthology(pk=1, title='Fantazmaty 5'), created_at=datetime.date(2026, 7, 29))
        other = Review(pk=2, title='B', anthology=Anthology(pk=2, title='nanoFantazje 2'), created_at=TODAY)
        for label, (series, candidates) in {
            'dokładnie na granicy odstępu': (recruitment(), [own]),
            'inny nabór cykliczny': (recruitment(), [other]),
            'odstęp niewpisany': (recruitment(value=None), [other]),
            'brak naboru': (None, [own]),
            'brak zgłoszeń': (recruitment(), []),
        }.items():
            with self.subTest(label):
                self.assertEqual(recent_submission_warning(series, candidates, today=TODAY), '')

    def test_newest_previous_submission_is_reported(self):
        book = Anthology(pk=1, title='Fantazmaty 5')
        older = Review(pk=7, title='Starszy', anthology=book, created_at=datetime.date(2026, 9, 1))
        newer = Review(pk=3, title='Nowszy', anthology=Anthology(pk=2, title='Fantazmaty 6'),
                       created_at=datetime.date(2026, 10, 1))
        warning = recent_submission_warning(recruitment(), [older, newer], today=TODAY)
        self.assertIn('#3 „Nowszy”, Fantazmaty 6', warning)


class SubjectTests(SimpleTestCase):
    """Temat maila naboru cyklicznego „Nabór: „Fantazmaty 7” – [tytuł opowiadania]”: prefiks „Nabór:”, tytuł
    opowiadania (także z cudzysłowami i myślnikami) i zawijanie długiego tematu nie zmieniają odczytanego tomu."""

    def test_volume_is_read_from_subject(self):
        long_title = 'Bardzo długi tytuł opowiadania o smokach ' * 4
        for subject, expected in {
            'Nabór: „Fantazmaty 7” – [tytul-opowiadania]': 'Fantazmaty 7',
            'Nabór: „Fantazmaty 7” – Smocza przystań': 'Fantazmaty 7',
            'Nabór: „nanoFantazje 3” – „Mały” smok – część 2': 'nanoFantazje 3',
            f'Nabór: „Fantazmaty 12” – {long_title}': 'Fantazmaty 12',
            'Nabór: "Fantazmaty 7" - Tytuł': 'Fantazmaty 7',
            'nabór:„Fantazmaty 7”–Tytuł': 'Fantazmaty 7',
            'Re: Nabór: „Fantazmaty 7” – Tytuł': 'Fantazmaty 7',
            'Nabór: „ Fantazmaty 7 ” – Tytuł': 'Fantazmaty 7',
        }.items():
            with self.subTest(subject):
                anthology = parse_message(12, named_mail(subject=subject))['anthology']
                self.assertEqual(anthology, expected)
                self.assertEqual(recruitment_for(anthology, [recruitment('Fantazmaty'), recruitment('nanoFantazje')]).name,
                                 expected.split()[0])


class CyclicRecruitmentDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.fantazmaty = CyclicRecruitment.objects.get(name='Fantazmaty')
        cls.nano = CyclicRecruitment.objects.get(name='nanoFantazje')
        cls.volume5 = Anthology.objects.create(title='Fantazmaty 5', status=Anthology.Status.READY)
        cls.volume6 = Anthology.objects.create(title='Fantazmaty 6', status=Anthology.Status.IN_PREPARATION)
        cls.nano2 = Anthology.objects.create(title='nanoFantazje 2', status=Anthology.Status.IN_PREPARATION)
        cls.other = Anthology.objects.create(title='Na pokład, psubraty', status=Anthology.Status.IN_PREPARATION)

    def previous(self, anthology=None, days=21, email='anna@example.com', title='Smoki', **extra):
        review = Review.objects.create(title=title, anthology=anthology or self.volume5, length=100, genre='Fantasy',
                                       email=email, author_first_name='Anna', author_last_name='Nowak', **extra)
        Review.objects.filter(pk=review.pk).update(created_at=timezone.localdate() - datetime.timedelta(days=days))
        return review


class SubmissionWarningTests(CyclicRecruitmentDataMixin, TestCase):
    """Ostrzeżenie przy zapisie zgłoszenia (texts.services.get_review_submission_warnings)."""

    def warnings(self, anthology=None, email='anna@example.com', title='Nowy tekst', **extra):
        return [w for w in get_review_submission_warnings(
            title=title, anthology_id=(anthology or self.volume6).pk, email=email, **extra)
            if w.startswith('Nabór cykliczny')]

    def test_migration_creates_both_recruitments(self):
        self.assertEqual([str(item) for item in CyclicRecruitment.objects.all()], ['Fantazmaty', 'nanoFantazje'])
        self.assertEqual([(r.name, r.interval_value, r.interval_unit) for r in CyclicRecruitment.objects.all()],
                         [('Fantazmaty', 3, 'months'), ('nanoFantazje', 3, 'months')])

    def test_recent_previous_text_in_series_warns(self):
        previous = self.previous()
        warning, = self.warnings()
        self.assertIn(f'#{previous.pk} „Smoki”, Fantazmaty 5', warning)
        self.assertIn('21 dni temu', warning)
        self.assertIn('minimalny odstęp to 3 miesiące', warning)

    def test_cases_without_warning(self):
        for label, (setup, kwargs) in {
            'poprzedni tekst starszy niż odstęp': (lambda: self.previous(days=120), {}),
            'inny autor': (lambda: self.previous(email='inny@example.com'), {}),
            'poprzedni tekst w innym naborze cyklicznym': (lambda: self.previous(anthology=self.nano2), {}),
            'zgłoszenie do antologii spoza naborów': (lambda: self.previous(), {'anthology': self.other}),
            'edycja tego samego zgłoszenia': (lambda: self.previous(), {'exclude_review_id': 'self'}),
        }.items():
            with self.subTest(label):
                review = setup()
                if kwargs.get('exclude_review_id') == 'self':
                    kwargs = {'exclude_review_id': review.pk}
                self.assertEqual(self.warnings(**kwargs), [])
                Review.objects.all().delete()

    def test_interval_can_be_disabled_or_shortened(self):
        self.previous(days=21)
        for (value, unit), warns in {(None, 'months'): False, (2, 'weeks'): False, (3, 'weeks'): False,
                                     (4, 'weeks'): True, (1, 'months'): True}.items():
            with self.subTest(value=value, unit=unit):
                CyclicRecruitment.objects.filter(pk=self.fantazmaty.pk).update(interval_value=value, interval_unit=unit)
                self.assertEqual(bool(self.warnings()), warns)

    def test_any_status_and_matching_author_record_count(self):
        author = Author.objects.create(first_name='Anna', last_name='Nowak', email='anna@example.com')
        for status in (Review.Status.REJECTED, Review.Status.ACCEPTED):
            with self.subTest(status):
                self.previous(status=status, author=author)
                self.assertEqual(len(self.warnings(email='', author=author)), 1)
                self.assertEqual(len(self.warnings(email='ANNA@Example.com')), 1)
                Review.objects.all().delete()

    def test_same_series_volume_also_counts(self):
        self.previous(anthology=self.volume6, title='Inny')
        self.assertEqual(len(self.warnings()), 1)
        self.previous(anthology=self.nano2, title='Mały')
        self.assertEqual(len(self.warnings(anthology=self.nano2)), 1)


class BulkImportWarningTests(CyclicRecruitmentDataMixin, TestCase):
    """Import wielu zgłoszeń: ostrzeżenie z bazy i kilka tekstów jednego autora w tym samym imporcie."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = create_superuser('cyclic-import')

    def form(self, *lines, anthology=None):
        return ReviewBulkImportForm({'anthology': (anthology or self.volume6).pk, 'records': '\n'.join(lines)},
                                    user=self.user)

    @staticmethod
    def line(title, email='anna@example.com'):
        return f'Anna Nowak;{title};fantasy;100;{email};;'

    def test_import_warnings(self):
        for label, (lines, anthology, expected) in {
            'dwa teksty jednego autora': ((self.line('A'), self.line('B')), None,
                                          ['Wiersz 2: nabór cykliczny Fantazmaty: ten sam autor ma w tym imporcie inny '
                                           'tekst (wiersz 1), a minimalny odstęp to 3 miesiące. Sprawdź przed dodaniem.']),
            'trzy teksty jednego autora': ((self.line('A'), self.line('B'), self.line('C')), None, 2),
            'różni autorzy': ((self.line('A'), self.line('B', 'inny@example.com')), None, []),
            'antologia spoza naborów': ((self.line('A'), self.line('B')), self.other, []),
        }.items():
            with self.subTest(label):
                form = self.form(*lines, anthology=anthology)
                # Ostrzeżenie wymaga potwierdzenia (formularz nieważny), bez ostrzeżeń import przechodzi.
                self.assertEqual(form.is_valid(), not expected)
                cyclic = [w for w in form.import_warnings if 'nabór cykliczny' in w]
                if isinstance(expected, int):
                    self.assertEqual([w[:9] for w in cyclic], ['Wiersz 2:', 'Wiersz 3:'])
                else:
                    self.assertEqual(cyclic, expected)

    def test_database_warning_is_prefixed_with_row(self):
        self.previous()
        form = self.form(self.line('Nowy'))
        self.assertFalse(form.is_valid())
        self.assertTrue(any(w.startswith('Wiersz 1: Nabór cykliczny Fantazmaty: autor przysłał') for w in form.import_warnings))

    def test_disabled_interval_has_no_batch_warning(self):
        CyclicRecruitment.objects.filter(pk=self.fantazmaty.pk).update(interval_value=None)
        form = self.form(self.line('A'), self.line('B'))
        self.assertTrue(form.is_valid(), form.errors)


class MailboxPreviewTests(CyclicRecruitmentDataMixin, TestCase):
    """Zgłoszenie ze skrzynki z tematem „Nabór: „Fantazmaty 6” – …”: ostrzeżenie w podglądzie przed zapisem."""

    def test_subject_with_any_volume_number_triggers_check(self):
        self.previous()
        box = MailboxConnection.objects.create(name='teksty', host='imap.example.test', username='t@example.test',
                                               encrypted_password='unused')
        for subject, anthology, warns in (('Nabór: „Fantazmaty 6” – Opowieść', self.volume6, True),
                                          ('Nabór: „Na pokład, psubraty” – Opowieść', self.other, False)):
            with self.subTest(subject):
                message = parse_message(12, named_mail(subject=subject))
                form, = prepare_forms([message], box, 1, create_superuser(f'u{anthology.pk}'))
                self.assertEqual(form.data['anthology'], anthology.pk)
                cyclic = [w for w in form.mail_display_warnings if 'Nabór cykliczny Fantazmaty' in w]
                self.assertEqual(len(cyclic), int(warns))
                if warns:
                    self.assertIn('poprzedni tekst 21 dni temu', cyclic[0])


class SettingsViewTests(CyclicRecruitmentDataMixin, TestCase):
    """Sekcja „Nabory cykliczne” na stronie „Dodaj do recenzji”: podgląd i zapis odstępów."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        MailboxConnection.objects.create(name='teksty', host='imap.example.test', username='t@example.test',
                                         encrypted_password='unused')

    def setUp(self):
        self.url = reverse('core:cyclic_recruitments')
        self.page = reverse('core:review_bulk_import')
        self.user = create_superuser('cyclic-admin')
        self.client.force_login(self.user)

    def payload(self, fantazmaty=('3', 'months'), nano=('2', 'weeks')):
        return {f'cyclic-{self.fantazmaty.pk}-interval_value': fantazmaty[0],
                f'cyclic-{self.fantazmaty.pk}-interval_unit': fantazmaty[1],
                f'cyclic-{self.nano.pk}-interval_value': nano[0], f'cyclic-{self.nano.pk}-interval_unit': nano[1]}

    def test_page_shows_collapsed_section_with_current_intervals(self):
        response = self.client.get(self.page)
        self.assertContains(response, 'id="nabory-cykliczne"')
        self.assertContains(response, 'Nabory cykliczne: Fantazmaty – 3 miesiące, nanoFantazje – 3 miesiące')
        self.assertFalse(IntakePanels(response.content).panels['cyclic'])
        self.assertContains(response, f'action="{self.url}"')

    def test_valid_save(self):
        for payload, expected in (
            (self.payload(), [(3, 'months'), (2, 'weeks')]),
            (self.payload(fantazmaty=('', 'months'), nano=('520', 'months')), [(None, 'months'), (520, 'months')]),
        ):
            with self.subTest(payload):
                response = self.client.post(self.url, payload, follow=True)
                self.assertRedirects(response, self.page)
                self.assertEqual([(r.interval_value, r.interval_unit) for r in CyclicRecruitment.objects.all()],
                                 expected)
                self.assertContains(response, 'Zapisano odstępy naborów cyklicznych: Fantazmaty – ')

    def test_invalid_values_are_not_saved(self):
        for label, payload in {
            'zero': self.payload(fantazmaty=('0', 'months')),
            'za duża liczba': self.payload(nano=('521', 'weeks')),
            'tekst': self.payload(nano=('trzy', 'weeks')),
            'zła jednostka': self.payload(nano=('3', 'days')),
        }.items():
            with self.subTest(label):
                response = self.client.post(self.url, payload)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(IntakePanels(response.content).panels['cyclic'])
                self.assertContains(response, 'Nie zapisano odstępów naborów cyklicznych', status_code=400)
                self.assertEqual([r.interval_value for r in CyclicRecruitment.objects.all()], [3, 3])

    def test_only_superuser_can_save_and_get_is_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.force_login(create_member('cyclic-member', 'Redaktor'))
        with patch('core.cyclic_recruitment.CyclicRecruitment.save') as save:
            self.assertEqual(self.client.post(self.url, self.payload()).status_code, 403)
        save.assert_not_called()
