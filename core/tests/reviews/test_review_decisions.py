"""Statusy i decyzje zgłoszeń: reguły przejść, powiadomienie autora i ograniczenia w adminie."""
from datetime import timedelta

from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from core.selectors.reviews import review_list_context
from core.services import reviews as service
from core.services.reviews import (
    MAX_BULK_REVIEWS,
    _validate_status_change,
    assign_reviewer,
    change_review_status,
    save_author_notification,
    unassign_reviewer,
)
from fantazmaty.testing import form_value
from texts.models import Review, ReviewAssignment, Text

from .support import ReviewFixtures

S = Review.Status


class ReviewStatusTests(ReviewFixtures):
    def change(self, user, status, review=None):
        return change_review_status(user=user, review_id=(review or self.review).pk, new_status=status)

    def assert_rejected(self, error, user, status, review=None):
        before = Review.objects.get(pk=(review or self.review).pk).status
        with self.assertRaises(error):
            self.change(user, status, review)
        self.assertEqual(Review.objects.get(pk=(review or self.review).pk).status, before)

    def test_manual_open_status_and_permissions(self):
        with self.assertRaises(PermissionDenied):
            change_review_status(user=self.other, review_id=self.review.pk, new_status='to_decide')
        change_review_status(user=self.coordinator, review_id=self.review.pk, new_status='to_decide')
        self.review.refresh_from_db()
        self.assertIsNone(self.review.decision_at)
        assign_reviewer(user=self.reviewer, review_id=self.review.pk)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, 'to_decide')
        unassign_reviewer(user=self.reviewer, review_id=self.review.pk)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, 'to_decide')
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:assigned_review_detail', args=[self.review.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'wyznaczona osoba')
        self.assertNotContains(response, 'name="status" value="to_decide"')

    def test_assignment_drives_regular_status_and_filters(self):
        self.assertEqual(self.review.get_status_display(), 'Do recenzji')
        assign_reviewer(user=self.reviewer, review_id=self.review.pk)
        self.review.refresh_from_db()
        self.assertEqual(self.review.get_status_display(), 'W recenzjach')
        self.client.force_login(self.coordinator)
        page = self.client.get(reverse('core:assigned_review_detail', args=[self.review.pk]))
        response = self.client.post(reverse('core:update_review_status', args=[self.review.pk]),
                                    {'status': 'to_decide', '_edit_version': form_value(page)})
        self.assertEqual(response.status_code, 302)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, 'to_decide')
        result = review_list_context(user=self.coordinator, params=QueryDict('status=to_decide'))
        self.assertEqual([r['pk'] for r in result['reviews']], [self.review.pk])
        self.assertIn(('to_decide', 'Do decyzji'), result['status_choices'])

    def test_superuser_corrects_rejection_before_the_author_is_notified(self):
        self.review.status = Review.Status.REJECTED
        self.review.save()
        _validate_status_change(user=self.admin, review=self.review, assignments=[],
                                new_status=Review.Status.ACCEPTED)
        self.review.author_notified_at = timezone.now()
        with self.assertRaises(ValidationError):
            _validate_status_change(user=self.admin, review=self.review, assignments=[],
                                    new_status=Review.Status.ACCEPTED)

    def test_accepted_decision_locked_after_author_notification(self):
        self.review.status = Review.Status.ACCEPTED
        self.review.author_notified_at = timezone.now()
        with self.assertRaisesMessage(ValidationError, 'powiadomiony o przyjęciu'):
            _validate_status_change(user=self.coordinator, review=self.review, assignments=[],
                                    new_status=Review.Status.REJECTED)

    def test_coordinator_decision_removes_unsubmitted_reading_and_dates_decision(self):
        reading = self.assign(self.reviewer, position=1)
        voted = self.assign(self.admin, position=2, opinion='yes')
        self.change(self.coordinator, S.ACCEPTED)
        self.review.refresh_from_db()
        self.assertEqual((self.review.status, self.review.decision_at), (S.ACCEPTED, timezone.localdate()))
        self.assertFalse(ReviewAssignment.objects.filter(pk=reading.pk).exists())
        self.assertTrue(ReviewAssignment.objects.filter(pk=voted.pk).exists())
        self.assertEqual(self.change(self.coordinator, S.ACCEPTED).status, S.ACCEPTED)

    def test_restore_from_decision_returns_to_matching_open_status(self):
        self.change(self.coordinator, S.TO_DECIDE)
        self.assertEqual(self.change(self.coordinator, 'restore').status, S.NEW)
        self.assert_rejected(ValidationError, self.coordinator, 'restore')
        self.assign(self.reviewer)
        self.change(self.coordinator, S.TO_DECIDE)
        self.assert_rejected(PermissionDenied, self.other, 'restore')
        self.assertEqual(self.change(self.coordinator, 'restore').status, S.IN_REVIEW)

    def test_status_rules_for_coordinators(self):
        self.assert_rejected(ValidationError, self.admin, 'unknown')
        self.assert_rejected(PermissionDenied, self.coordinator, S.WITHDRAWN)
        self.assert_rejected(PermissionDenied, self.coordinator, S.NEW)
        for status in (S.ACCEPTED, S.REJECTED, S.WITHDRAWN):
            closed = self.make_review(status=status)
            with self.subTest(status=status):
                self.assert_rejected(ValidationError, self.coordinator, S.TO_DECIDE, closed)
        withdrawn = self.make_review(status=S.WITHDRAWN)
        self.assert_rejected(PermissionDenied, self.coordinator, S.ACCEPTED, withdrawn)
        rejected = self.make_review(status=S.REJECTED)
        self.assert_rejected(ValidationError, self.coordinator, S.ACCEPTED, rejected)
        hidden = self.make_review(is_hidden=True)
        self.assert_rejected(PermissionDenied, self.coordinator, S.ACCEPTED, hidden)
        archived = self.make_review(old_reviews=True)
        self.assert_rejected(ValidationError, self.admin, S.ACCEPTED, archived)
        copied = self.make_review(status=S.ACCEPTED, copied_text=Text.objects.create(title='Kopia', length=1))
        self.assert_rejected(ValidationError, self.admin, S.REJECTED, copied)

    def test_superuser_open_statuses_must_match_assignments(self):
        self.assert_rejected(ValidationError, self.admin, S.IN_REVIEW)
        self.assign(self.reviewer)
        self.assert_rejected(ValidationError, self.admin, S.NEW)
        self.assertEqual(self.change(self.admin, S.IN_REVIEW).status, S.IN_REVIEW)
        self.change(self.admin, S.WITHDRAWN)
        self.review.refresh_from_db()
        self.assertEqual(self.review.decision_at, timezone.localdate())
        self.assertEqual(self.change(self.admin, S.IN_REVIEW).decision_at, None)

class AuthorNotificationRuleTests(ReviewFixtures):
    def notify(self, review=None, when=None, user=None):
        return save_author_notification(user=user or self.admin, review_id=(review or self.review).pk,
                                        author_notified_at=when)

    def test_notification_only_for_due_uncopied_decisions_by_superuser(self):
        with self.assertRaises(PermissionDenied):
            self.notify(user=self.coordinator)
        with self.assertRaisesMessage(ValidationError, 'po przyjęciu albo odrzuceniu'):
            self.notify()
        scheduled = self.make_review(status=S.ACCEPTED, decision_at=timezone.localdate() + timedelta(days=2))
        with self.assertRaisesMessage(ValidationError, 'Nie nadszedł'):
            self.notify(scheduled)
        copied = self.make_review(status=S.ACCEPTED, copied_text=Text.objects.create(title='Kopia', length=1))
        with self.assertRaisesMessage(ValidationError, 'przeniesiony'):
            self.notify(copied)
        rejected = self.make_review(status=S.REJECTED, decision_at=timezone.localdate())
        yesterday = timezone.localdate() - timedelta(days=1)
        self.assertEqual(self.notify(rejected, yesterday).author_notified_at, yesterday)
        self.assertIsNone(self.notify(rejected, None).author_notified_at)


class ReviewIdListTests(SimpleTestCase):
    def test_bulk_selection_must_be_a_bounded_list_of_positive_ids(self):
        for value in (None, '1,2', b'1', [], ['1'], [0], [True], [service.MAX_DATABASE_ID + 1],
                      list(range(1, MAX_BULK_REVIEWS + 2))):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValidationError):
                service._normalise_review_ids(value)
        self.assertEqual(service._normalise_review_ids((3, 1, 3)), [1, 3])


class ReviewAdminRestrictionTests(ReviewFixtures):
    def setUp(self):
        super().setUp()
        Review.objects.filter(pk=self.review.pk).update(status='in_review')
        self.review.refresh_from_db()
        self.request = RequestFactory().get('/')
        self.request.user = self.admin

    def model_admin(self):
        from texts.admin import ReviewAdmin

        return ReviewAdmin(Review, site)

    def test_hidden_flag_is_read_only_for_current_reviews(self):
        self.assertIn('is_hidden', self.model_admin().get_readonly_fields(self.request, self.review))

    def test_review_with_submitted_opinion_cannot_be_deleted(self):
        self.assertTrue(self.model_admin().has_delete_permission(self.request, self.review))
        self.assign(self.reviewer, opinion='yes')
        self.assertFalse(self.model_admin().has_delete_permission(self.request, self.review))
        self.assertNotIn('delete_selected', self.model_admin().get_actions(self.request))
