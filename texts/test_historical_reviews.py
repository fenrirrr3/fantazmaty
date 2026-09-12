from django.test import TestCase
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model
from authors.models import Author
from people.models import Person
from texts.models import Review, ReviewAssignment, Anthology

class HistoricalReviewTests(TestCase):
    def setUp(self):
        self.anthology = Anthology.objects.create(title='Archiwum')

    def review(self, **kwargs):
        data = dict(title='Wspólny tekst', anthology=self.anthology,
                    author_first_name='Jan', author_last_name='Testowy',
                    old_reviews=True, length=None, email='', genre='')
        data.update(kwargs)
        obj = Review(**data)
        obj.full_clean()
        obj.save()
        return obj

    def test_missing_data_only_in_archive(self):
        obj = self.review()
        obj.old_reviews = False
        with self.assertRaises(ValidationError) as error:
            obj.full_clean()
        self.assertTrue({'length', 'genre', 'email'} <= set(error.exception.message_dict))
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Review.objects.filter(pk=obj.pk).update(old_reviews=False)

    def test_primary_and_coauthors(self):
        first = Author.objects.create(first_name='Jan', last_name='Testowy')
        second = Author.objects.create(first_name='Anna', last_name='Testowa')
        obj = self.review(author=first)
        obj.coauthors.add(first, second)
        self.assertEqual([a.pk for a in obj.display_authors], [first.pk, second.pk])
        self.assertEqual(second.coauthored_review_submissions.get(), obj)

    def test_historical_reviewer_without_account_and_unknown_dates(self):
        person = Person.objects.create(first_name='Jan', last_name='Historyczny', is_active=False)
        obj = self.review()
        assignment = ReviewAssignment(review=obj, historical_person=person, position=1,
                                      opinion='yes', assigned_at=None, opinion_changed_at=None)
        assignment.full_clean()
        assignment.save()
        assignment.refresh_from_db()
        self.assertIsNone(assignment.user_id)
        self.assertIsNone(assignment.assigned_at)
        self.assertIsNone(assignment.opinion_changed_at)
        self.assertEqual(assignment.reviewer_display_name, str(person))
        self.assertFalse(ReviewAssignment.objects.for_statistics().exists())
        self.assertEqual(get_user_model().objects.count(), 0)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ReviewAssignment.objects.create(review=obj, historical_person=person, position=2)

    def test_current_review_rejects_historical_profile(self):
        obj = self.review(old_reviews=False, length=100, genre='fantastyka', email='test@example.com')
        person = Person.objects.create(first_name='Jan', last_name='Historyczny', is_active=False)
        assignment = ReviewAssignment(review=obj, historical_person=person, position=1)
        with self.assertRaises(ValidationError):
            assignment.full_clean()

    def test_archive_projection_keeps_author_permissions(self):
        from core.selectors.reviews import _project_reviews
        from django.template.loader import render_to_string
        user = get_user_model().objects.create_superuser(username='admin', password='test')
        author = Author.objects.create(first_name='Anna', last_name='Współautorka')
        obj = self.review()
        obj.coauthors.add(author)
        person = Person.objects.create(first_name='Jan', last_name='Historyczny', is_active=False)
        ReviewAssignment.objects.create(review=obj, historical_person=person, position=1, opinion='yes')
        obj.selector_assignments = list(obj.assignments.all())
        visible = _project_reviews([obj], user=user, include_authors=True, allow_self_assignment=False)[0]
        hidden = _project_reviews([obj], user=user, include_authors=False, allow_self_assignment=False)[0]
        self.assertEqual([a.pk for a in visible['authors']], [author.pk])
        self.assertNotIn('authors', hidden)
        # Both templates must compile with the new coauthor loop.
        from django.template.loader import get_template
        get_template('core/review_list.html')
        get_template('core/assigned_review_detail.html')
