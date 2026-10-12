"""Import zgłoszeń: odrzucane wiersze parsera, zmiany danych między walidacją a zapisem i ekran importu zbiorczego."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from authors.models import Author
from core.forms import ReviewBulkImportForm
from core.services.review_import_parser import (MAX_IMPORT_RECORDS, named_submission, named_submission_blocks,
                                                parse_review_records)
from core.services.reviews import import_reviews
from core.tests.recruitment.support import TemporaryActivitySpoolMixin
from fantazmaty.testing import create_superuser
from texts.models import Anthology, Review

RECORD = 'Ala Autor;Nowa opowieść;fantasy;3000;ala@example.com;123456789;Nabór'
NO_GENRE = 'Ala Autor;Bez gatunku;;3000;ala@example.com;123456789;Nabór'


class ReviewImportParserGapTests(SimpleTestCase):
    def test_rejected_rows_report_line_and_reason(self):
        """Każdy błędny wiersz trafia do listy błędów z numerem wiersza i nie tworzy rekordu."""
        named_bad_consent = ('Imię: Ala\nNazwisko: Autor\nTytuł opowiadania: T\nGatunek: fantasy\n'
                             'Liczba znaków ze spacjami: 3000\nAdres e-mail: ala@example.com\nNewsletter: spam')
        cases = {
            'pusty import': ('', 'Wklej przynajmniej jedno zgłoszenie.'),
            'zgody w formularzu z etykietami': (named_bad_consent, 'Wiersz 1: Zgody: dozwolone są'),
            'zgody w starszym formacie': (RECORD + ';spam', 'Wiersz 1: Zgody: dozwolone są'),
            'nadmiarowe pola starszego formatu': (RECORD + ';premierach;dopisek',
                                                  'Wiersz 1: starszy format zawiera dodatkowe pola'),
            'autor bez nazwiska': ('Ala;Tytuł;fantasy;3000;ala@example.com;123;Nabór',
                                   'Wiersz 1: autora zapisz jako IMIĘ NAZWISKO.'),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name):
                records, errors = parse_review_records(value)
                self.assertEqual(records, [])
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith(message), errors)

    def test_import_over_record_limit_is_refused_as_a_whole(self):
        value = '\n'.join(RECORD.replace('Nowa', f'Nowa {index}') for index in range(MAX_IMPORT_RECORDS + 1))
        with self.assertRaisesMessage(ValidationError, f'najwyżej {MAX_IMPORT_RECORDS} niepustych wierszy'):
            parse_review_records(value)

    def test_input_without_labelled_header_is_not_a_named_submission(self):
        """Puste wejście i zwykły wiersz CSV nie są traktowane jak formularz z etykietami."""
        for value in ('', '   \n', RECORD):
            with self.subTest(value=value):
                self.assertIsNone(named_submission(value))
                self.assertIsNone(named_submission_blocks(value))


class ImportRaceMixin:
    """Zmienia dane po pierwszej walidacji formularza w serwisie (jak równoległa operacja innej osoby)."""

    def import_with_change(self, change, records=RECORD):
        self.form = form = ReviewBulkImportForm({'anthology': self.book.pk, 'records': records}, user=self.admin)
        original = ReviewBulkImportForm.is_valid
        calls = []

        def is_valid(checked):
            result = original(checked)
            calls.append(result)
            if len(calls) == 1:
                change()
            return result

        with patch.object(ReviewBulkImportForm, 'is_valid', autospec=True, side_effect=is_valid):
            return form, import_reviews(user=self.admin, form=form)


class ReviewImportRaceTests(ImportRaceMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('import-admin')

    def setUp(self):
        self.book = Anthology.objects.create(title='Nabór')

    def test_changes_between_validation_and_save_cancel_whole_import(self):
        """Zmiana naboru lub autora w trakcie importu przerywa go bez zapisu zgłoszeń."""
        def close_book():
            Anthology.objects.filter(pk=self.book.pk).update(status=Anthology.Status.READY)

        def delete_books():
            Anthology.objects.all().delete()

        def delete_author():
            Author.objects.filter(email='ala@example.com').delete()

        def add_author():
            Author.objects.create(first_name='Ala', last_name='Autor', email='ala@example.com')

        cases = (
            ('nabór zamknięty', close_book, False, 'Wybrany nabór nie jest już dostępny.'),
            ('brak naborów', delete_books, False, 'Najpierw utwórz nabór do antologii.'),
            ('usunięty autor', delete_author, True, 'Dane autorów zmieniły się podczas importu.'),
            ('nowy autor', add_author, False, 'Powiązania autorów zmieniły się podczas importu.'),
        )
        for name, change, existing_author, message in cases:
            with self.subTest(name):
                Author.objects.all().delete()
                Anthology.objects.filter(pk=self.book.pk).update(status=Anthology.Status.IN_PREPARATION)
                if not Anthology.objects.filter(pk=self.book.pk).exists():
                    self.book = Anthology.objects.create(title='Nabór')
                if existing_author:
                    add_author()
                with self.assertRaisesMessage(ValidationError, message):
                    self.import_with_change(change)
                self.assertFalse(Review.objects.exists())

    def test_warning_found_in_second_check_refreshes_form(self):
        """Autor trafia na czarną listę między dwiema walidacjami: formularz dostaje ostrzeżenie do potwierdzenia."""
        def blacklist():
            Author.objects.create(first_name='Ala', last_name='Autor', email='ala@example.com', is_blacklisted=True)

        with self.assertRaisesMessage(ValidationError, 'Wykryto ostrzeżenia'):
            self.import_with_change(blacklist)
        self.assertFalse(Review.objects.exists())
        # Formularz widoku dostaje ostrzeżenia z drugiej walidacji i nowy token potwierdzenia.
        self.assertTrue(any('czarnej liście' in warning for warning in self.form.import_warnings))
        self.assertTrue(self.form['submission_warnings_token'].value())
        self.assertEqual(self.form['records'].value(), RECORD)

    def test_record_without_genre_is_refused_already_in_preview(self):
        """Gatunek jest wymagany dla bieżącego zgłoszenia – błąd z numerem wiersza pojawia się w podglądzie,
        zanim powstanie token importu (wcześniej podgląd przechodził, a import kończył się błędem)."""
        form = ReviewBulkImportForm({'anthology': self.book.pk, 'records': NO_GENRE}, user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('Wiersz 1, gatunek: pole wymagane dla bieżącego zgłoszenia.', str(form.errors))
        self.assertFalse(Review.objects.exists())

    def test_import_requires_bound_bulk_form(self):
        for form in (ReviewBulkImportForm(user=self.admin), object()):
            with self.subTest(form=type(form).__name__):
                with self.assertRaisesMessage(ValidationError, 'Import wymaga wypełnionego formularza.'):
                    import_reviews(user=self.admin, form=form)


class ReviewBulkSubmitViewGapTests(TemporaryActivitySpoolMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('bulk-admin')
        cls.book = Anthology.objects.create(title='Nabór')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse('core:review_bulk_submit')

    def preview(self, records):
        return self.client.post(self.url, {'anthology': self.book.pk, 'records': records,
                                           'import_action': 'preview'})

    def test_forged_preview_token_only_shows_preview(self):
        response = self.client.post(self.url, {'anthology': self.book.pk, 'records': RECORD,
                                               'import_action': 'import', 'preview_token': 'podrobiony'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['preview_token'])
        self.assertEqual(len(response.context['preview_rows']), 1)
        self.assertFalse(Review.objects.exists())

    def test_service_error_after_confirmed_preview_is_shown_on_form(self):
        preview = self.preview(RECORD)
        self.assertEqual(preview.status_code, 200)
        # Błąd zgłoszony przez usługę dopiero przy zapisie (np. walidacja modelu) trafia do formularza.
        failure = ValidationError('Wiersz 1: Pole wymagane dla bieżącego zgłoszenia.')
        with patch('core.views.reviews.import_reviews', side_effect=failure):
            response = self.client.post(self.url, {'anthology': self.book.pk, 'records': RECORD,
                                                   'import_action': 'import',
                                                   'preview_token': preview.context['preview_token']})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Wiersz 1: Pole wymagane dla bieżącego zgłoszenia.',
                      response.context['bulk_form'].non_field_errors())
        self.assertFalse(Review.objects.exists())

    def test_preview_lists_at_most_thousand_rows(self):
        records = '\n'.join(RECORD.replace('Nowa', f'Nowa {index}') for index in range(1005))
        response = self.preview(records)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(response.context['preview_rows']), 1000)
        self.assertIn(f'najwyżej {MAX_IMPORT_RECORDS}', str(response.context['bulk_form'].errors))
