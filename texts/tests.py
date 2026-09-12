"""Testy modeli; definicje modeli znajdują się wyłącznie w models.py."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from people.models import Person
from .models import MAX_REVIEWERS, Anthology, AnthologyTask, Review, ReviewAssignment, Reviewers


class RestoredModelsTests(TestCase):
    def setUp(self):
        self.anthology = Anthology.objects.create(title="Antologia")
        self.review = Review.objects.create(
            anthology=self.anthology, title="Tekst", length=100,
            author_first_name="Jan", author_last_name="Testowy",
            email="autor@example.com", genre="fantastyka",
        )

    def test_six_positions_are_enforced_by_database(self):
        self.assertEqual(ReviewAssignment.MAX_REVIEWERS, MAX_REVIEWERS)
        for position in range(1, 7):
            ReviewAssignment.objects.create(review=self.review, position=position)
        notes = Reviewers.objects.create(review=self.review)
        self.assertFalse(notes.has_free_slot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewAssignment.objects.create(review=self.review, position=7)

    def test_duplicate_position_and_reviewer_are_rejected(self):
        user = get_user_model().objects.create_user(username="recenzent")
        ReviewAssignment.objects.create(review=self.review, user=user, position=1)
        for values in ({"position": 1}, {"position": 2, "user": user}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                ReviewAssignment.objects.create(review=self.review, **values)

    def test_deleted_user_keeps_opinion_and_position(self):
        user = get_user_model().objects.create_user(username="recenzent")
        assignment = ReviewAssignment.objects.create(
            review=self.review, user=user, position=1, opinion=ReviewAssignment.Opinion.YES,
        )
        user.delete()
        assignment.refresh_from_db()
        self.assertIsNone(assignment.user_id)
        self.assertEqual(assignment.opinion, ReviewAssignment.Opinion.YES)
        self.assertEqual(assignment.position, 1)

    def test_opinion_date_changes_only_when_opinion_is_saved(self):
        old_date, new_date = date(2026, 1, 1), date(2026, 2, 1)
        assignment = ReviewAssignment.objects.create(
            review=self.review, position=1, opinion_changed_at=old_date,
        )
        with patch("texts.models.timezone.localdate", return_value=new_date):
            assignment.opinion = ReviewAssignment.Opinion.YES
            assignment.notes = "Uwagi"
            assignment.save(update_fields=["notes"])
            assignment.refresh_from_db()
            self.assertEqual(assignment.opinion, ReviewAssignment.Opinion.READING)
            self.assertEqual(assignment.opinion_changed_at, old_date)
            assignment.opinion = ReviewAssignment.Opinion.YES
            assignment.save(update_fields=["opinion"])
        assignment.refresh_from_db()
        self.assertEqual(assignment.opinion_changed_at, new_date)

    def test_archive_is_excluded_from_statistics_and_assignment(self):
        ReviewAssignment.objects.create(review=self.review, position=1)
        self.review.old_reviews = True
        self.review.save(update_fields=["old_reviews"])
        self.assertEqual(Review.objects.count(), 1)
        self.assertFalse(Review.objects.for_statistics().exists())
        self.assertFalse(ReviewAssignment.objects.for_statistics().exists())
        self.assertFalse(Reviewers.objects.create(review=self.review).has_free_slot)

    def test_commission_date_uses_only_saved_fields(self):
        person = Person.objects.create(first_name="Jan", last_name="Testowy", email="team@example.com")
        task = self.anthology.typesetting_task
        self.assertEqual(self.anthology.production_tasks.count(), 3)
        task.assigned_to = person
        task.status = AnthologyTask.Status.COMMISSIONED
        with patch("texts.models.timezone.localdate", return_value=date(2026, 1, 1)):
            task.save(update_fields=["assigned_to", "status"])
        task.status = AnthologyTask.Status.NOT_COMMISSIONED
        task.save(update_fields=["assigned_to"])
        task.refresh_from_db()
        self.assertEqual(task.status, AnthologyTask.Status.COMMISSIONED)
        self.assertEqual(task.commissioned_at, date(2026, 1, 1))
