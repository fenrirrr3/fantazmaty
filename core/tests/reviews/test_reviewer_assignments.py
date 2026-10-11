"""Oddawanie i zwalnianie recenzji: rezygnacja recenzenta, zwolnienie miejsca przez koordynatora, zaległe recenzje."""
from django.core.exceptions import ValidationError
from django.urls import reverse

from core.permissions import can_self_assign_reviews
from core.selectors.reports import overdue_review_rows
from core.services.reviews import release_reviewer_slot, unassign_reviewer
from texts.models import Review, ReviewAssignment

from .support import ReviewFixtures


class ReviewerAssignmentTests(ReviewFixtures):
    def test_submitted_review_cannot_be_relinquished(self):
        assignment = ReviewAssignment.objects.create(review=self.review, user=self.reviewer, position=1,
                                                     opinion='yes', notes='Ocena pozostaje')
        with self.assertRaises(ValidationError):
            unassign_reviewer(user=self.reviewer, review_id=self.review.pk)
        assignment.refresh_from_db()
        self.assertEqual(assignment.notes, 'Ocena pozostaje')
        self.client.force_login(self.reviewer)
        page = self.client.get(reverse('core:assigned_review_detail', args=[self.review.pk]))
        self.assertFalse(page.context['can_unassign_review'])
        self.assertNotContains(page, reverse('core:unassign_reviewer', args=[self.review.pk]))

    def test_unsubmitted_review_can_be_relinquished(self):
        assignment = self.assign(self.reviewer)
        self.assertTrue(unassign_reviewer(user=self.reviewer, review_id=self.review.pk))
        self.assertFalse(ReviewAssignment.objects.filter(pk=assignment.pk).exists())

    def test_role_change_keeps_own_current_reviews_accessible(self):
        """Chroni przed utratą dostępu do własnych recenzji po odebraniu roli Recenzent."""
        ReviewAssignment.objects.create(review=self.review, user=self.reviewer, position=1)
        self.reviewer.person_profile.roles.clear()
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse('core:my_reviews')).status_code, 200)
        self.assertFalse(can_self_assign_reviews(self.reviewer))

    def test_coordinator_releases_reading_slot(self):
        Review.objects.filter(pk=self.review.pk).update(status=Review.Status.IN_REVIEW)
        item = self.assign(self.reviewer, 30)
        release_reviewer_slot(user=self.coordinator, review_id=self.review.pk, assignment_id=item.pk)
        self.assertFalse(ReviewAssignment.objects.filter(pk=item.pk).exists())
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.NEW)

    def test_submitted_opinion_slot_cannot_be_released(self):
        item = self.assign(self.reviewer, 30, opinion='yes')
        with self.assertRaises(ValidationError):
            release_reviewer_slot(user=self.coordinator, review_id=self.review.pk, assignment_id=item.pk)

    def test_overdue_reviews_are_listed_in_inactivity_report(self):
        Review.objects.filter(pk=self.review.pk).update(title='Zgłoszenie zaległe', status=Review.Status.IN_REVIEW)
        late = self.assign(self.reviewer, 22)
        self.assign(self.admin, 10, position=2)
        rows = overdue_review_rows()
        self.assertEqual([row['review'].pk for row in rows], [self.review.pk])
        self.assertEqual(rows[0]['days'], 22)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:workflow_inactivity')).content.decode()
        self.assertIn('Zgłoszenie zaległe', page)
        page = self.client.get(reverse('core:workflow_inactivity'), {'mode': 'reviews'}).content.decode()
        self.assertIn('Zgłoszenie zaległe', page)
        self.assertNotIn('workflow-inactivity-table', page)
        ReviewAssignment.objects.filter(pk=late.pk).update(opinion='yes')
        self.assertEqual(overdue_review_rows(), [])
