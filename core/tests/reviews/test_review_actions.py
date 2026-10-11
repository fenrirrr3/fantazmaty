"""Akcje na stronie zgłoszenia: przypisanie, opinia, ostrzeżenia, decyzja, powiadomienie, przeniesienie i operacje zbiorcze."""
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from fantazmaty.testing import edit_token
from texts.models import Review, ReviewAssignment, Text

from .support import ReviewFixtures

S = Review.Status
GENERIC_ERROR = 'Nie udało się wykonać operacji'


class ReviewPageActionTests(ReviewFixtures):
    # Odmowy dla tras tylko superusera sprawdza macierz w core/tests/security/test_route_access.py.
    def post(self, user, name, data=None, review=None, args=()):
        review = review or self.review
        self.client.force_login(user)
        payload = {'_edit_version': edit_token(user, review), **(data or {})}
        return self.client.post(reverse(f'core:{name}', args=[review.pk, *args]), payload)

    def message(self, response):
        """Najnowszy komunikat (wcześniejsze czekają w sesji, bo przekierowań nie odwiedzamy)."""
        return [str(message) for message in get_messages(response.wsgi_request)][-1]

    def detail_url(self, review=None):
        return reverse('core:assigned_review_detail', args=[(review or self.review).pk])

    def test_reviewer_assigns_and_relinquishes_from_page(self):
        response = self.post(self.reviewer, 'assign_reviewer')
        self.assertRedirects(response, self.detail_url(), fetch_redirect_response=False)
        self.assertEqual(self.message(response), 'Przypisano cię do recenzji.')
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, S.IN_REVIEW)
        response = self.post(self.reviewer, 'unassign_reviewer')
        self.assertEqual(self.message(response), 'Usunięto twoje przypisanie do recenzji.')
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, S.NEW)
        self.assertEqual(self.post(self.coordinator, 'assign_reviewer').status_code, 403)
        self.assertFalse(self.review.assignments.exists())

    def test_closed_review_actions_report_generic_error_to_team_and_details_to_superuser(self):
        closed = self.make_review(status=S.ACCEPTED, title='Zamknięte')
        self.assign(self.reviewer, review=closed)
        for name in ('assign_reviewer', 'unassign_reviewer'):
            with self.subTest(name=name):
                response = self.post(self.reviewer, name, review=closed)
                self.assertEqual(response.status_code, 302)
                self.assertIn(GENERIC_ERROR, self.message(response))
        item = self.assign(self.admin, position=2, review=closed)
        response = self.post(self.admin, 'release_reviewer_slot', review=closed, args=[item.pk])
        self.assertEqual(self.message(response), 'Archiwalna lub zakończona recenzja jest zablokowana.')
        self.assertEqual(closed.assignments.count(), 2)

    def test_slot_release_from_page_by_review_coordinator_only(self):
        item = self.assign(self.reviewer)
        response = self.post(self.other, 'release_reviewer_slot', args=[item.pk])
        self.assertIn('tylko koordynator recenzji', self.message(response))
        response = self.post(self.coordinator, 'release_reviewer_slot', args=[item.pk + 100])
        self.assertIn(GENERIC_ERROR, self.message(response))
        response = self.post(self.coordinator, 'release_reviewer_slot', args=[item.pk])
        self.assertEqual(self.message(response), 'Zwolniono miejsce recenzenta.')
        self.assertFalse(ReviewAssignment.objects.filter(pk=item.pk).exists())

    def test_assigned_reviewer_saves_opinion_and_invalid_form_is_returned(self):
        self.assertEqual(self.post(self.reviewer, 'assigned_review_detail', {'opinion': 'yes', 'notes': 'X'})
                         .status_code, 403)
        item = self.assign(self.reviewer)
        page = self.post(self.reviewer, 'assigned_review_detail', {'opinion': 'yes', 'notes': ''})
        self.assertEqual(page.status_code, 400)
        self.assertTrue(page.context['opinion_form'].errors)
        response = self.post(self.reviewer, 'assigned_review_detail',
                             {'opinion': 'yes', 'notes': 'Dobre', 'content_warnings': 'Przemoc'})
        self.assertEqual(self.message(response), 'Zapisano opinię.')
        item.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual((item.opinion, item.notes, self.review.content_warnings), ('yes', 'Dobre', 'Przemoc'))

    def test_locked_review_rejects_opinion_and_warning_changes(self):
        locked = self.make_review(status=S.TO_DECIDE, title='Zablokowane')
        item = self.assign(self.reviewer, review=locked)
        Review.objects.filter(pk=locked.pk).update(status=S.REJECTED)
        for name, data in (('assigned_review_detail', {'opinion': 'no', 'notes': 'Za późno'}),
                           ('update_review_content_warnings', {'content_warnings': 'Za późno'})):
            with self.subTest(name=name):
                response = self.post(self.reviewer, name, data, review=locked)
                self.assertRedirects(response, self.detail_url(locked), fetch_redirect_response=False)
                self.assertIn('zablokowana', self.message(response))
        item.refresh_from_db()
        locked.refresh_from_db()
        self.assertEqual((item.opinion, locked.content_warnings), ('reading', ''))

    def test_content_warnings_by_coordinator_or_assigned_reviewer(self):
        data = {'content_warnings': 'Ostrzeżenie'}
        self.assertEqual(self.post(self.reviewer, 'update_review_content_warnings', data).status_code, 403)
        response = self.post(self.coordinator, 'update_review_content_warnings', data)
        self.assertEqual(self.message(response), 'Zapisano ostrzeżenia dotyczące treści.')
        self.review.refresh_from_db()
        self.assertEqual(self.review.content_warnings, 'Ostrzeżenie')

    def test_author_notification_after_decision(self):
        data = {'author_notified': 'on', 'author_notified_at': timezone.localdate().isoformat()}
        page = self.post(self.admin, 'update_author_notification', data)
        self.assertEqual(page.status_code, 400)
        self.assertIn('po przyjęciu albo odrzuceniu', str(page.context['author_notification_form'].errors))
        Review.objects.filter(pk=self.review.pk).update(status=S.ACCEPTED, decision_at=timezone.localdate())
        self.assertEqual(self.post(self.admin, 'update_author_notification',
                                   {'author_notified': 'on', 'author_notified_at': 'zła'}).status_code, 400)
        response = self.post(self.admin, 'update_author_notification', data)
        self.assertEqual(self.message(response), 'Zapisano oznaczenie powiadomienia autora.')
        self.review.refresh_from_db()
        self.assertEqual(self.review.author_notified_at, timezone.localdate())

    def test_status_form_accepts_only_decisions_and_restore(self):
        response = self.post(self.coordinator, 'update_review_status', {'status': S.WITHDRAWN})
        self.assertEqual(self.message(response), 'Wybierz prawidłowy status zgłoszenia.')
        response = self.post(self.coordinator, 'update_review_status', {'status': 'restore'})
        self.assertIn(GENERIC_ERROR, self.message(response))
        response = self.post(self.coordinator, 'update_review_status', {'status': S.REJECTED})
        self.assertEqual(self.message(response), 'Zapisano decyzję dotyczącą tekstu.')
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, S.REJECTED)

    def test_transfer_to_publication_from_page(self):
        Review.objects.filter(pk=self.review.pk).update(status=S.ACCEPTED, author_notified_at=timezone.localdate())
        response = self.post(self.admin, 'copy_review_to_text')
        self.assertIn('potwierdź otrzymanie umowy', self.message(response))
        self.assertFalse(Text.objects.exists())
        response = self.post(self.admin, 'copy_review_to_text', {'contract_received': 'yes'})
        self.assertEqual(self.message(response), 'Tekst jest dodany do procesu wydawniczego.')
        self.review.refresh_from_db()
        self.assertEqual(self.review.copied_text.title, self.review.title)

    def test_superuser_restores_wrongly_hidden_review(self):
        def hidden(**values):
            return self.make_review(is_hidden=True, status=S.REJECTED, decision_at=timezone.localdate(),
                                    author_notified_at=timezone.localdate(), **values)

        def state(review):
            review.refresh_from_db()
            return review.is_hidden, review.status, review.decision_at, review.author_notified_at

        review = hidden()
        unchanged = state(review)
        self.assertEqual(self.post(self.admin, 'release_hidden_review', {'confirm': 'yes'}).status_code, 404)
        refused = [(review, {}, 'Potwierdź przywrócenie'),
                   (hidden(old_reviews=True), {'confirm': 'yes'}, 'Archiwalnego lub skopiowanego'),
                   (hidden(copied_text=Text.objects.create(title='Kopia', length=1)), {'confirm': 'yes'},
                    'Archiwalnego lub skopiowanego')]
        for item, data, message in refused:
            with self.subTest(message=message, review=item.pk):
                before = state(item)
                response = self.post(self.admin, 'release_hidden_review', data, review=item)
                self.assertRedirects(response, self.detail_url(item), fetch_redirect_response=False)
                self.assertIn(message, self.message(response))
                self.assertEqual(state(item), before)
        self.client.force_login(self.admin)
        stale = self.client.post(reverse('core:release_hidden_review', args=[review.pk]),
                                 {'confirm': 'yes', '_edit_version': 'nieaktualny'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(state(review), unchanged)
        response = self.post(self.admin, 'release_hidden_review', {'confirm': 'yes'}, review=review)
        self.assertIn('Przywrócono widoczność', self.message(response))
        self.assertEqual(state(review), (False, S.NEW, None, None))
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(self.detail_url(review)).status_code, 200)

    def test_superuser_detail_suggests_author_profile_matching_email_and_name(self):
        Review.objects.filter(pk=self.review.pk).update(phone_number='500 500 500')
        author = Author.objects.create(first_name='Autor', last_name='Test', email='AUTHOR@example.com',
                                       phone_number='111', has_contract=True)
        self.client.force_login(self.admin)
        page = self.client.get(self.detail_url())
        self.assertEqual(page.context['matching_author'], {'pk': author.pk, 'has_contract': True})
        self.assertTrue(page.context['author_has_contract'])
        self.assertEqual(page.context['copy_profile_phone'], '111')
        self.assertTrue(page.context['copy_phone_differs'])
        Author.objects.filter(pk=author.pk).update(last_name='Inny')
        self.assertIsNone(self.client.get(self.detail_url()).context['matching_author'])


class ReviewBulkActionPageTests(ReviewFixtures):
    url = reverse('core:bulk_review_action')

    def setUp(self):
        super().setUp()
        self.second = self.make_review(title='Drugie')
        self.client.force_login(self.admin)

    def bulk(self, ids, **data):
        response = self.client.post(self.url, {'selected_reviews': [str(i) for i in ids], **data})
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        return response, messages[-1] if messages else None

    def test_invalid_selection_or_form_is_reported_without_changes(self):
        ids = [self.review.pk, self.second.pk]
        cases = [([], {'action': 'export'}, 'Zaznacz przynajmniej'),
                 (['abc'], {'action': 'export'}, 'nieprawidłowy identyfikator'),
                 (['0'], {'action': 'export'}, 'nieprawidłowy identyfikator'),
                 (['1'] * 1001, {'action': 'export'}, 'najwyżej 1000'),
                 (ids, {'action': ''}, 'To pole jest wymagane'),
                 (ids, {'action': 'change_status'}, 'Wybierz nowy status'),
                 ([*ids, self.second.pk + 100], {'action': 'export'}, 'Nie odnaleziono'),
                 (ids, {'action': 'change_status', 'status': S.IN_REVIEW}, 'przynajmniej jednego przydziału')]
        for selected, data, expected in cases:
            with self.subTest(expected=expected):
                response, message = self.bulk(selected, **data)
                self.assertRedirects(response, reverse('core:review_list'), fetch_redirect_response=False)
                self.assertIn(expected, message)
        self.assertEqual(set(Review.objects.values_list('status', flat=True)), {S.NEW})

    def test_export_and_status_change_for_selection(self):
        response, _ = self.bulk([self.review.pk, self.second.pk], action='export')
        self.assertEqual(response['Content-Type'].split(';')[0], 'text/csv')
        self.assertIn('Drugie', response.content.decode('utf-8-sig'))
        response, message = self.bulk([self.review.pk, self.second.pk], action='change_status', status=S.REJECTED)
        self.assertEqual(message, 'Wykonano operację dla 2 zgłoszeń.')
        self.assertEqual(set(Review.objects.values_list('status', flat=True)), {S.REJECTED})


class MyReviewsAccessTests(ReviewFixtures):
    def test_my_reviews_require_reviewer_role_or_own_history(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('core:my_reviews')).status_code, 403)
        self.assign(self.other, opinion='yes')
        page = self.client.get(reverse('core:my_reviews'), {'view': 'unknown', 'opinion': ['yes', 'zła']})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['selected_view'], 'active')
        self.assertEqual(page.context['selected_opinions'], [])
        page = self.client.get(reverse('core:my_reviews'), {'view': 'completed', 'opinion': ['yes', 'zła'],
                                                             'anthology': self.book.pk})
        self.assertEqual(page.context['selected_opinions'], ['yes'])
        self.assertEqual([a.review_id for a in page.context['assignments']], [self.review.pk])
