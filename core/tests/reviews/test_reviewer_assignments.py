"""Przydziały recenzentów: przypisanie, rezygnacja, zwolnienie miejsca przez koordynatora, opinie i zaległe recenzje."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from core.permissions import can_self_assign_reviews
from core.selectors.reports import overdue_review_rows
from core.services.reviews import (
    assign_reviewer,
    release_reviewer_slot,
    save_review_content_warnings,
    save_reviewer_opinion,
    unassign_reviewer,
)
from fantazmaty.testing import create_member
from people.models import Vacation
from texts.models import Review, ReviewAssignment, Text

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

    def test_unsubmitted_review_can_be_relinquished_and_status_follows_remaining_reviewers(self):
        self.assertFalse(unassign_reviewer(user=self.reviewer, review_id=self.review.pk))
        Review.objects.filter(pk=self.review.pk).update(status=Review.Status.IN_REVIEW)
        self.assign(self.admin, position=1)
        mine = self.assign(self.reviewer, position=2)
        self.assertTrue(unassign_reviewer(user=self.reviewer, review_id=self.review.pk))
        self.assertFalse(ReviewAssignment.objects.filter(pk=mine.pk).exists())
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.IN_REVIEW)
        self.assertTrue(unassign_reviewer(user=self.admin, review_id=self.review.pk))
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.NEW)

    def test_review_coordinator_releases_only_unsubmitted_current_slot(self):
        Review.objects.filter(pk=self.review.pk).update(status=Review.Status.IN_REVIEW)
        item = self.assign(self.reviewer, 30)
        voted = self.assign(self.admin, 30, opinion='yes', position=2)
        with self.assertRaises(PermissionDenied):
            release_reviewer_slot(user=self.other, review_id=self.review.pk, assignment_id=item.pk)
        with self.assertRaisesMessage(ValidationError, 'Odśwież stronę'):
            release_reviewer_slot(user=self.coordinator, review_id=self.review.pk, assignment_id=item.pk + 100)
        with self.assertRaisesMessage(ValidationError, 'oddał już opinię'):
            release_reviewer_slot(user=self.coordinator, review_id=self.review.pk, assignment_id=voted.pk)
        release_reviewer_slot(user=self.coordinator, review_id=self.review.pk, assignment_id=item.pk)
        self.assertFalse(ReviewAssignment.objects.filter(pk=item.pk).exists())
        ReviewAssignment.objects.filter(pk=voted.pk).update(opinion='reading')
        release_reviewer_slot(user=self.coordinator, review_id=self.review.pk, assignment_id=voted.pk)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.NEW)

    def test_role_change_keeps_own_current_reviews_accessible(self):
        """Chroni przed utratą dostępu do własnych recenzji po odebraniu roli Recenzent."""
        ReviewAssignment.objects.create(review=self.review, user=self.reviewer, position=1)
        self.reviewer.person_profile.roles.clear()
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse('core:my_reviews')).status_code, 200)
        self.assertFalse(can_self_assign_reviews(self.reviewer))

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

    def test_only_available_reviewers_assign_to_open_current_reviews(self):
        with self.assertRaises(PermissionDenied):
            assign_reviewer(user=self.coordinator, review_id=self.review.pk)
        blocked = [
            (PermissionDenied, {'is_hidden': True}),
            (ValidationError, {'old_reviews': True}),
            (ValidationError, {'status': Review.Status.ACCEPTED}),
            (ValidationError, {'copied_text': Text.objects.create(title='Kopia', length=1)}),
        ]
        for error, values in blocked:
            review = self.make_review(**values)
            with self.subTest(values=list(values)), self.assertRaises(error):
                assign_reviewer(user=self.reviewer, review_id=review.pk)
            self.assertFalse(review.assignments.exists())
        Vacation.objects.create(person=self.reviewer.person_profile, start_date=timezone.localdate(),
                                until_revoked=True)
        with self.assertRaises(ValidationError):
            assign_reviewer(user=self.reviewer, review_id=self.review.pk)

    def test_assignment_takes_first_free_position_until_slots_are_full(self):
        self.assign(self.admin, position=1)
        self.assign(self.coordinator, position=3)
        first = assign_reviewer(user=self.reviewer, review_id=self.review.pk)
        self.assertEqual((first.position, first.opinion), (2, 'reading'))
        self.assertEqual(assign_reviewer(user=self.reviewer, review_id=self.review.pk).pk, first.pk)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.IN_REVIEW)
        full = self.make_review(title='Pełne')
        for position in range(1, ReviewAssignment.MAX_REVIEWERS + 1):
            self.assign(create_member(f'slot{position}', 'Recenzent'), position=position, review=full)
        with self.assertRaisesMessage(ValidationError, 'komplet recenzentów'):
            assign_reviewer(user=self.reviewer, review_id=full.pk)

class ReviewerOpinionRuleTests(ReviewFixtures):
    def test_opinion_saved_only_by_assigned_reviewer_with_valid_choice(self):
        with self.assertRaises(PermissionDenied):
            save_reviewer_opinion(user=self.reviewer, review_id=self.review.pk, opinion='yes', notes='Uwagi')
        item = self.assign(self.reviewer)
        for opinion, notes in (('reading', 'Uwagi'), ('bad', 'Uwagi'), ('yes', '')):
            with self.subTest(opinion=opinion, notes=notes), self.assertRaises(ValidationError):
                save_reviewer_opinion(user=self.reviewer, review_id=self.review.pk, opinion=opinion, notes=notes)
        save_reviewer_opinion(user=self.reviewer, review_id=self.review.pk, opinion='yes', notes='Dobre',
                              content_warnings='Przemoc')
        item.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual((item.opinion, item.notes, item.opinion_changed_at), ('yes', 'Dobre', timezone.localdate()))
        self.assertEqual(self.review.content_warnings, 'Przemoc')

    def test_unchanged_opinion_keeps_original_change_date(self):
        earlier = timezone.localdate() - timedelta(days=5)
        item = self.assign(self.reviewer, opinion='yes')
        ReviewAssignment.objects.filter(pk=item.pk).update(opinion_changed_at=earlier)
        save_reviewer_opinion(user=self.reviewer, review_id=self.review.pk, opinion='yes', notes='Poprawione')
        item.refresh_from_db()
        self.assertEqual((item.notes, item.opinion_changed_at), ('Poprawione', earlier))

    def test_content_warnings_by_coordinator_or_assigned_reviewer_only(self):
        with self.assertRaises(PermissionDenied):
            save_review_content_warnings(user=self.reviewer, review_id=self.review.pk, content_warnings='X')
        save_review_content_warnings(user=self.coordinator, review_id=self.review.pk, content_warnings='Koordynator')
        self.assign(self.reviewer)
        save_review_content_warnings(user=self.reviewer, review_id=self.review.pk, content_warnings='Recenzent')
        self.review.refresh_from_db()
        self.assertEqual(self.review.content_warnings, 'Recenzent')
