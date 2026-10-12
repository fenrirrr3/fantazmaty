"""Raport wielokrotnych przypisań osoby do tekstu: sortowanie kolumn i para poprawiona w międzyczasie."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.assignment_integrity import assignment_conflict_details, assignment_conflicts
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment as A


class AssignmentConflictSortingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('raport-admin', 'raport@example.test', 'test')
        cls.anna = User.objects.create_user('anna', first_name='Anna', last_name='Zięba')
        cls.bartek = User.objects.create_user('bartek', first_name='Bartek', last_name='Adamski')
        book = Anthology.objects.create(title='Antologia')
        cls.alpha = Text.objects.create(title='Akacja', anthology=book, length=100)
        cls.beta = Text.objects.create(title='Brzoza', anthology=book, length=100)
        # Akacja: Anna dwa razy w tej samej roli; Brzoza: Bartek w trzech rolach.
        A.objects.create(text=cls.alpha, role='editor', assigned_to=cls.anna, is_current=False)
        A.objects.create(text=cls.alpha, role='editor', assigned_to=cls.anna, execution_number=2)
        for role in ('editor', 'proofreader_1', 'proofreader_3'):
            A.objects.create(text=cls.beta, role=role, assigned_to=cls.bartek)

    def titles(self, sort):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:data_integrity'), {'tab': 'assignments', 'sort': sort})
        self.assertEqual(response.status_code, 200)
        return [row['title'] for row in response.context['assignment_rows']]

    def test_every_column_sorts_both_ways(self):
        cases = {
            'title': ['Akacja', 'Brzoza'], '-title': ['Brzoza', 'Akacja'],
            'person': ['Akacja', 'Brzoza'], '-person': ['Brzoza', 'Akacja'],
            'reason': ['Akacja', 'Brzoza'], '-reason': ['Brzoza', 'Akacja'],
            'assignments': ['Akacja', 'Brzoza'], '-assignments': ['Brzoza', 'Akacja'],
        }
        for sort, expected in cases.items():
            with self.subTest(sort=sort):
                self.assertEqual(self.titles(sort), expected)

    def test_pair_resolved_after_counting_is_skipped(self):
        groups = list(assignment_conflicts())
        A.objects.filter(text=self.alpha, execution_number=2).update(assigned_to=self.bartek)
        rows = assignment_conflict_details(groups)
        self.assertEqual([row['title'] for row in rows], ['Brzoza'])
