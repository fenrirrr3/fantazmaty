"""Luki pokrycia nadzoru: kontrola spójności procesu, zasługi w publikacji, strona antologii i zadania na pulpicie."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.anthology_tasks import NewTaskPersonForm
from core.supervision import anthology_credits, integrity_issues
from core.tests.anthologies.support import AnthologyPageData
from fantazmaty.testing import create_member
from people.models import Person
from texts.models import Anthology, AnthologyTask, Review, ReviewAssignment, Text
from workflow.models import WorkflowHandoff, WorkflowRepetition, WorkflowRoleAssignment, WorkflowStage


def labels(rows, detail):
    return [row['label'] for row in rows if detail in row['detail']]


class IntegrityGapTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('spojnosc-redaktor', 'Redaktor')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email=None)
        cls.book = Anthology.objects.create(title='Zbiór spójności')

    def text(self, title):
        text = Text.objects.create(title=title, anthology=self.book, length=10)
        text.authors.add(self.author)
        text.workflow_stages.update(is_current=False)
        return text

    def test_contradictory_stage_records_are_reported(self):
        """Każdy rodzaj sprzecznego zapisu etapów trafia do listy problemów przy właściwym tekście."""
        today = timezone.localdate()
        unassigned = self.text('Bez wykonawcy')
        WorkflowStage.objects.create(text=unassigned, stage_type='editing', started_at=today)
        ended = self.text('Data bez zakończenia')
        WorkflowStage.objects.create(text=ended, stage_type='editing', started_at=today, ended_at=today)
        doubled = self.text('Dwa otwarte')
        for iteration in (1, 2):
            WorkflowStage.objects.create(text=doubled, stage_type='first_proofreading', iteration=iteration)
        orphan = self.text('Brak następcy')
        WorkflowStage.objects.create(text=orphan, stage_type='editing_control', started_at=today, ended_at=today,
                                     is_completed=True)
        empty_role = self.text('Pusty przydział')
        WorkflowRoleAssignment.objects.create(text=empty_role, role='editor', assigned_to=None)
        rows = integrity_issues()
        expected = {
            'Bez wykonawcy': 'Rozpoczęty etap bez wykonawcy',
            'Data bez zakończenia': 'Sprzeczny zapis zakończenia',
            'Dwa otwarte': 'Kilka otwartych etapów tego samego rodzaju',
            'Brak następcy': 'Brak następnego etapu po zakończeniu',
        }
        for title, label in expected.items():
            with self.subTest(title):
                self.assertIn(label, labels(rows, title))
        self.assertNotIn('Przydział do nieaktywnej osoby', labels(rows, 'Pusty przydział'))

    def test_queue_with_later_step_released_too_early_is_reported(self):
        text = self.text('Kolejka')
        run = WorkflowRepetition.objects.create(text=text)
        for position, kind in enumerate(('first_proofreading', 'second_proofreading')):
            WorkflowStage.objects.create(text=text, stage_type=kind, repetition=run, queue_position=position)
        found = labels(integrity_issues(), f'kolejka #{run.pk}')
        self.assertIn('Przedwcześnie udostępniony etap kolejki', found)
        self.assertNotIn('Otwarta kolejka bez dostępnego etapu', found)


class CreditGapTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('zaslugi-redaktor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Zbiór zasług')

    def names(self, credits):
        return [row['name'] for row in credits]

    def test_vacant_assignments_and_tasks_are_not_credited(self):
        """Przekazanie z nieobsadzonego przydziału i gotowe zadanie bez osoby nie dają pustej pozycji zasług."""
        text = Text.objects.create(title='Tekst z przekazaniem', anthology=self.book, length=10)
        vacant = WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=None)
        stage = WorkflowStage.objects.create(text=text, stage_type='editing', assignment=vacant)
        vacant_new = WorkflowRoleAssignment.objects.create(text=text, role='verifier_1', assigned_to=None)
        WorkflowHandoff.objects.create(text=text, stage=stage, previous_assignment=vacant, new_assignment=vacant_new)
        AnthologyTask.objects.filter(anthology=self.book, task_type='blurb').update(status='ready', assigned_to=None)
        self.assertEqual(anthology_credits(self.book, text=text, include_assignments=True), [])
        self.assertEqual(anthology_credits(self.book), [])


class AnthologyPageGapTests(AnthologyPageData, TestCase):
    def test_novel_redirects_and_unknown_operation_is_refused(self):
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        response = self.client.get(reverse('core:anthology_detail', args=[novel.pk]))
        self.assertRedirects(response, reverse('core:novel_detail', args=[novel.pk]), fetch_redirect_response=False)
        response = self.client.post(self.url, {'action': 'wymysl', '_edit_version': self.token()})
        self.assertEqual((response.status_code, response.content.decode()), (400, 'Nieznana operacja.'))

    def test_duplicate_person_added_concurrently_is_reported_on_form(self):
        """Ktoś dodał tę samą osobę między sprawdzeniem formularza a zapisem: nic nie zostaje utworzone."""
        original = NewTaskPersonForm.is_valid
        calls = []

        def is_valid(form):
            result = original(form)
            calls.append(result)
            if len(calls) == 1:
                Person.objects.create(first_name='Nowa', last_name='blurb', is_external=True, is_active=False)
            return result

        with patch.object(NewTaskPersonForm, 'is_valid', autospec=True, side_effect=is_valid):
            response = self.new_person('blurb')
        self.assertEqual(response.status_code, 400)
        form = response.context['new_person_form']
        self.assertIn('Dane zadania lub spisu osób zmieniły się', str(form.non_field_errors()))
        self.assertIn('first_name', form.errors)
        self.assertEqual(Person.objects.filter(first_name='Nowa', last_name='blurb').count(), 1)
        self.assertIsNone(AnthologyTask.objects.get(anthology=self.book, task_type='blurb').assigned_to)

    def test_saving_invalid_form_directly_is_refused(self):
        form = NewTaskPersonForm({}, anthology=self.book)
        with self.assertRaisesMessage(ValidationError, 'Popraw dane nowej osoby.'):
            form.save(user=self.manager)


class ReviewerDashboardTasksTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer = create_member('tylko-recenzent', 'Recenzent')
        book = Anthology.objects.create(title='Nabór pulpitu')
        review = Review.objects.create(title='Czytam', anthology=book, length=10, genre='fantasy',
                                       email='a@example.com')
        ReviewAssignment.objects.create(review=review, user=cls.reviewer, position=1, opinion='reading')

    def test_reviewer_only_sees_review_tasks_and_unknown_kind_is_refused(self):
        self.client.force_login(self.reviewer)
        url = reverse('core:dashboard_tasks')
        # Recenzent bez innych ról widzi czytane zgłoszenia zamiast etapów tekstów.
        for kind, count in (('active', 1), ('reserved', 0)):
            with self.subTest(kind=kind):
                payload = self.client.get(url, {'kind': kind}).json()
                self.assertEqual(payload['html'].count('dashboard-list-item'), count)
                self.assertEqual('Czytam' in payload['html'], bool(count))
        response = self.client.get(url, {'kind': 'wszystko'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'error': 'Nieprawidłowy rodzaj zadań.'})
