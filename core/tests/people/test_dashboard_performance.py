"""Wydajność pulpitu: stała liczba zapytań niezależna od liczby tekstów osoby."""
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fantazmaty.testing import create_member
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S

# Liczba zapytań pulpitu koordynatora z pracą w toku, oczekującą, rezerwacją i zaległościami
# (sesja, konto, uprawnienia, klasyfikacja tekstów, listy pulpitu, belka zaległości, recenzje, urlopy, nabór).
COORDINATOR_DASHBOARD_QUERIES = 27


class DashboardQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.coordinator = create_member('pulpit-wydajnosc', 'Koordynator')
        cls.book = Anthology.objects.create(title='Wydajność')

    def add_work(self, count):
        """Teksty koordynatora w każdym stanie pulpitu: redakcja w toku (zaległa), redaktor czeka na
        weryfikację, rezerwacja weryfikacji."""
        today = timezone.localdate()
        start = Text.objects.count()
        for number in range(start, start + count):
            active = Text.objects.create(title=f'W toku {number}', length=1, anthology=self.book)
            editor = A.objects.create(text=active, role='editor', assigned_to=self.coordinator)
            S.objects.create(text=active, stage_type='editing', assignment=editor, started_at=today - timedelta(days=40))
            waiting = Text.objects.create(title=f'Czeka {number}', length=1, anthology=self.book)
            editor = A.objects.create(text=waiting, role='editor', assigned_to=self.coordinator)
            S.objects.create(text=waiting, stage_type='editing', assignment=editor, started_at=today,
                             ended_at=today, is_completed=True)
            S.objects.create(text=waiting, stage_type='first_verification')
            reserved = Text.objects.create(title=f'Rezerwacja {number}', length=1, anthology=self.book)
            A.objects.create(text=reserved, role='verifier_1', assigned_to=self.coordinator)
            S.objects.create(text=reserved, stage_type='editing', started_at=today)

    def dashboard_queries(self):
        self.client.force_login(self.coordinator)
        self.client.get(reverse('core:home'))  # rozgrzewka: sesja i pamięć podręczna ról
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        return response, len(queries)

    def test_coordinator_dashboard_query_budget(self):
        """Pulpit koordynatora mieści się w budżecie zapytań i pokazuje wszystkie rodzaje pracy."""
        self.add_work(3)
        self.client.force_login(self.coordinator)
        self.client.get(reverse('core:home'))
        with self.assertNumQueries(COORDINATOR_DASHBOARD_QUERIES):
            response = self.client.get(reverse('core:home'))
        self.assertEqual((response.context['active_stage_count'], response.context['reserved_assignment_count']),
                         (6, 3))
        self.assertEqual(response.context['overdue']['count'], 3)

    def test_query_count_does_not_grow_with_texts(self):
        """1 i 10 tekstów w każdym stanie – ta sama liczba zapytań (brak zapytań na wiersz)."""
        counts = []
        for total in (1, 10):
            self.add_work(total - Text.objects.filter(title__startswith="W toku").count())
            response, count = self.dashboard_queries()
            self.assertEqual(response.context['active_stage_count'], 2 * total)
            self.assertEqual(response.context['reserved_assignment_count'], total)
            counts.append(count)
        self.assertEqual(counts[0], counts[1])
