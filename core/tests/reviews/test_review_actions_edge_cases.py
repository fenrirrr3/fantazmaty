"""Luki pokrycia na stronie zgłoszenia i w danych recenzji: błędy serwisu, walidacja bez zapisu i raporty."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import QueryDict
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.edit_versions import version_of
from core.selectors.reports import reviewer_activity_context
from core.selectors.reviews import review_list_context
from core.services.reviews import resolve_publication_authors
from core.source_reviews import link_source_review, unlink_source_review
from fantazmaty.testing import edit_token
from people.models import Person
from texts.models import Review, ReviewAssignment, Text

from .support import ReviewFixtures

GENERIC_ERROR = 'Nie udało się wykonać operacji'


class ReviewPageServiceErrorTests(ReviewFixtures):
    def post(self, user, name, data, review=None):
        review = review or self.review
        self.client.force_login(user)
        return self.client.post(reverse(f'core:{name}', args=[review.pk]),
                                {'_edit_version': edit_token(user, review), **data})

    def test_opinion_is_saved_even_when_review_metadata_is_incomplete(self):
        """Brak gatunku lub e-maila w zgłoszeniu (sprawa koordynatora) nie blokuje opinii recenzenta
        (wcześniej recenzent dostawał tylko ogólny komunikat błędu)."""
        item = self.assign(self.reviewer)
        for field, value in (('genre', ''), ('email', '')):
            Review.objects.filter(pk=self.review.pk).update(**{field: value})
            with self.subTest(field):
                response = self.post(self.reviewer, 'assigned_review_detail',
                                     {'opinion': 'yes', 'notes': f'Bez {field}', 'content_warnings': 'przemoc'})
                self.assertEqual(response.status_code, 302)
                item.refresh_from_db()
                self.review.refresh_from_db()
                self.assertEqual((item.opinion, item.notes, self.review.content_warnings),
                                 ('yes', f'Bez {field}', 'przemoc'))

    def test_unblock_reading_errors_are_shown_as_messages(self):
        """Odblokowanie pobierania ze strony: brak blokady (walidacja) i brak uprawnień – komunikat, bez zmian."""
        Review.objects.filter(pk=self.review.pk).update(reading_blocked_at=timezone.now())
        cases = (
            ('nie koordynator', self.other, 'tylko koordynator recenzji'),
            ('nie zablokowane', self.admin, 'nie jest zablokowane'),
        )
        for name, user, fragment in cases:
            if name == 'nie zablokowane':
                Review.objects.filter(pk=self.review.pk).update(reading_blocked_at=None)
            with self.subTest(name):
                response = self.post(user, 'unblock_review_reading', {})
                self.assertRedirects(response, reverse('core:assigned_review_detail', args=[self.review.pk]),
                                     fetch_redirect_response=False)
                self.assertIn(fragment, [str(m) for m in get_messages(response.wsgi_request)][-1])
        self.review.refresh_from_db()
        self.assertIsNone(self.review.reading_unblocked_by)

    def test_service_refusal_of_opinion_lands_on_the_form(self):
        """Odmowa serwisu przy poprawnym formularzu (np. zgłoszenie zamknięte w międzyczasie) trafia
        do błędów formularza, bez zapisu opinii."""
        item = self.assign(self.reviewer)
        with patch('core.views.reviews.save_reviewer_opinion', side_effect=ValidationError('Za późno.')):
            response = self.post(self.reviewer, 'assigned_review_detail', {'opinion': 'yes', 'notes': 'Dobre'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['opinion_form'].non_field_errors(), [GENERIC_ERROR + '. Sprawdź aktualny status recenzji, przypisanie oraz wprowadzone dane.'])
        item.refresh_from_db()
        self.assertEqual(item.opinion, 'reading')

    def test_content_warnings_refused_for_transferred_review_and_invalid_input(self):
        """Ostrzeżeń nie zapisuje zgłoszenie przeniesione do procesu ani niepoprawny formularz."""
        transferred = self.make_review(title='Przeniesione')
        Review.objects.filter(pk=transferred.pk).update(copied_text=Text.objects.create(title='Przeniesione',
                                                                                         length=10))
        cases = (
            ('przeniesione zgłoszenie', transferred, 'Ostrzeżenie',
             'Zgłoszenie zostało już przeniesione do procesu wydawniczego.'),
            ('znak NUL', self.review, 'Zły\x00znak', None),
        )
        for name, review, value, message in cases:
            with self.subTest(name):
                response = self.post(self.admin, 'update_review_content_warnings', {'content_warnings': value},
                                     review=review)
                self.assertEqual(response.status_code, 400)
                form = response.context['review_content_warnings_form']
                if message:
                    self.assertEqual(form.non_field_errors(), [message])
                else:
                    self.assertIn('content_warnings', form.errors)
                review.refresh_from_db()
                self.assertEqual(review.content_warnings, '')


class PublicationAuthorValidationTests(ReviewFixtures):
    def test_validation_only_mode_prepares_author_without_saving(self):
        """persist=False (formularz admina) zwraca uzupełnionego autora, ale nie zmienia bazy."""
        author = Author.objects.create(first_name='Autor', last_name='Test', email='author@example.com',
                                       phone_number='111', has_contract=False)
        Review.objects.filter(pk=self.review.pk).update(phone_number='222 333 444')
        self.review.refresh_from_db()
        with transaction.atomic():
            resolved, coauthors = resolve_publication_authors(self.review, contract_received=True,
                                                              update_author_phone=True, persist=False)
        self.assertEqual((resolved.pk, resolved.has_contract, resolved.phone_number), (author.pk, True, '222 333 444'))
        self.assertEqual(coauthors, [])
        author.refresh_from_db()
        self.assertEqual((author.has_contract, author.phone_number), (False, '111'))


class ReviewRowsAndReportTests(ReviewFixtures):
    def test_review_list_rows_support_len_and_single_item(self):
        rows = review_list_context(user=self.admin, params=QueryDict())['reviews']
        self.assertEqual(len(rows), Review.objects.count())
        self.assertEqual(rows[0]['pk'], rows[0:1][0]['pk'])

    def test_reviewer_report_names_deleted_and_historical_reviewers(self):
        """Raport opinii zachowuje opinie usuniętych kont i profili historycznych z archiwum."""
        archived = self.make_review(title='Archiwalne', old_reviews=True, status=Review.Status.REJECTED,
                                    author_first_name='Zofia', author_last_name='Unikat')
        historical = Person.objects.create(first_name='Hanna', last_name='Dawna', email='hanna@example.com')
        ReviewAssignment.objects.create(review=archived, historical_person=historical, position=1, opinion='yes')
        gone = get_user_model().objects.create_user('gone')
        ReviewAssignment.objects.create(review=archived, user=gone, position=2, opinion='no')
        gone.delete()
        params = QueryDict(mutable=True)
        params.update({'archive': 'all', 'q': 'Unikat', 'sort': 'position'})
        context = reviewer_activity_context(user=self.admin, params=params)
        rows = context['activity_rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['reviewer_name'], 'Hanna Dawna')
        sorted_rows, _ = rows.sort_table(RequestFactory().get('/', {'sort': 'position'}))
        self.assertEqual([row['reviewer_name'] for row in sorted_rows[0:2]], ['Hanna Dawna', 'Usunięte konto'])
        self.assertEqual([str(person) for person in context['reviewers']], ['Hanna Dawna'])


class SourceReviewLinkGapTests(ReviewFixtures):
    def test_relinking_same_review_is_idempotent_and_stale_unlink_is_refused(self):
        text = Text.objects.create(title='Tekst', length=1000, anthology=self.book)
        Review.objects.filter(pk=self.review.pk).update(status=Review.Status.ACCEPTED, copied_text=text,
                                                        decision_at=timezone.localdate())
        version = version_of(text)
        self.assertEqual(link_source_review(user=self.admin, text_id=text.pk, review_id=self.review.pk,
                                            confirm_mismatch=True).copied_text_id, text.pk)
        # Ponowne powiązanie tego samego zgłoszenia nie zmienia tekstu ani jego wersji edycji.
        self.assertEqual(version_of(text), version)
        with self.assertRaisesMessage(ValidationError, 'Powiązanie już się zmieniło'):
            unlink_source_review(user=self.admin, text_id=text.pk, review_id=self.review.pk + 1)
        self.review.refresh_from_db()
        self.assertEqual((self.review.copied_text_id, self.review.publication_detached), (text.pk, False))

