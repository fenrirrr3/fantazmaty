"""Statusy i decyzje zgłoszeń: ręczne statusy, blokada po powiadomieniu autora i ograniczenia w adminie."""
from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import QueryDict
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from core.selectors.reviews import review_list_context
from core.services.reviews import _validate_status_change, assign_reviewer, change_review_status, unassign_reviewer
from fantazmaty.testing import form_value
from texts.models import Review

from .support import ReviewFixtures


class ReviewStatusTests(ReviewFixtures):
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
