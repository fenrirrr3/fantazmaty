"""Kontrola zgłoszenia przed zapisem (texts.services): tożsamość autora, czarna lista i możliwe duplikaty."""
from django.test import TestCase

from authors.models import Author, BlacklistEntry
from texts.models import Anthology, Review
from texts.services import MAX_DUPLICATES_IN_WARNING, find_matching_authors, get_review_submission_warnings


class SubmissionWarningTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Nabór')

    def review(self, title='Smoki', email='autor@example.test'):
        return Review.objects.create(title=title, anthology=self.book, length=100, genre='Fantasy',
                                     email=email, author_first_name='Jan', author_last_name='Autor')

    def test_unsaved_author_is_a_programming_error(self):
        with self.assertRaisesMessage(ValueError, 'musi być zapisany'):
            get_review_submission_warnings(title='Smoki', author=Author(first_name='Jan', last_name='Autor'))

    def test_without_identity_or_title_nothing_is_matched(self):
        self.review()
        self.assertFalse(find_matching_authors(email='  ').exists())
        for label, kwargs in {
            'bez e-maila i autora': {'title': 'Smoki'},
            'pusty tytuł': {'title': '   ', 'email': 'autor@example.test'},
        }.items():
            with self.subTest(label):
                self.assertEqual(get_review_submission_warnings(**kwargs), [])

    def test_independent_blacklist_entry_is_reported(self):
        entry = BlacklistEntry.objects.create(email='zablokowany@example.test')
        BlacklistEntry.objects.create(email='nieaktywny@example.test', is_active=False)
        warnings = get_review_submission_warnings(title='Nowy', email='Zablokowany@Example.test')
        self.assertEqual(len(warnings), 1)
        self.assertIn(f'niezależnego wpisu czarnej listy (#{entry.pk})', warnings[0])
        self.assertEqual(get_review_submission_warnings(title='Nowy', email='nieaktywny@example.test'), [])

    def test_long_duplicate_list_is_shortened(self):
        reviews = [self.review(title='  SMOKI ') for _ in range(MAX_DUPLICATES_IN_WARNING + 2)]
        warning, = get_review_submission_warnings(title='smoki', email='autor@example.test')
        self.assertIn(f'Liczba pasujących zgłoszeń: {len(reviews)}.', warning)
        self.assertIn('Pozostałych pasujących rekordów: 2.', warning)
        listed = [f'#{review.pk} ' for review in reviews]
        self.assertEqual(sum(item in warning for item in listed), MAX_DUPLICATES_IN_WARNING)
