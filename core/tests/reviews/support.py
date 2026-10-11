"""Wspólne dane testów recenzji: zespół z rolami recenzenckimi i jedno otwarte zgłoszenie."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Review, ReviewAssignment, Reviewers


class ReviewFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.coordinator = create_member('coord', 'Koordynator recenzji')
        cls.reviewer = create_member('reader', 'Recenzent')
        cls.other = create_member('other', 'Koordynator korekty')
        cls.book = Anthology.objects.create(title='Nabór')

    def setUp(self):
        self.review = self.make_review()

    def make_review(self, **values):
        data = dict(anthology=self.book, title='Tekst', author_first_name='Autor', author_last_name='Test',
                    email='author@example.com', genre='fantasy', length=1000)
        data.update(values)
        return Review.objects.create(**data)

    def assign(self, user, days=0, opinion=Reviewers.Opinion.READING, position=1, review=None):
        """Przypisuje recenzenta z datą przypisania cofniętą o podaną liczbę dni."""
        item = ReviewAssignment.objects.create(review=review or self.review, user=user, opinion=opinion,
                                               position=position)
        if days:
            ReviewAssignment.objects.filter(pk=item.pk).update(assigned_at=timezone.now() - timedelta(days=days))
        return item
