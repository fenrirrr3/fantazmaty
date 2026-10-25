"""Panel administratora procesu: formularz dat etapu, błędy z usług i wyścigów, ręczny status,
dodawanie brakującego etapu, notatki przypisań i przywracanie rezerwacji W1."""
from datetime import timedelta
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from fantazmaty.testing import create_member, create_superuser, form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from texts.models import Anthology, Text
from workflow.admin_add_stage import add_missing_stage
from workflow.admin_stage_dates import preserve_stage_order, set_stage_dates
from workflow.admin_status import set_admin_status
from workflow.archive_executions import SOURCE
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages
from workflow.reservation_repair import restore_first_verification_reservation

from .support import EditingInProgressFixtures, add_roles, importing


class StageDatesGapTests(TestCase):
    """Formularz „Ustaw daty / zakończ etap” dla trwającej pierwszej korekty."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.start = cls.today - timedelta(days=10)
        cls.admin = create_superuser('daty-admin')
        cls.member = create_member('daty-korektor', 'Korektor')
        add_roles(cls.member, 'Redaktor')
        cls.text = Text.objects.create(title='Tekst', length=100,
                                       anthology=Anthology.objects.create(title='Antologia'))
        cls.assignment = A.objects.create(text=cls.text, role='proofreader_1', assigned_to=cls.member)
        cls.stage = S.objects.create(text=cls.text, assignment=cls.assignment,
                                     stage_type='first_proofreading', started_at=cls.start)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])

    def post(self, **data):
        return self.client.post(self.url, {'version': version_of(self.text), **data})

    def assert_unchanged(self):
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)
        self.assertIsNone(self.stage.ended_at)
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_form_requires_finish_flag_and_both_dates(self):
        cases = {
            'data zakończenia bez przekazania': (
                {'started_at': self.start.isoformat(), 'ended_at': self.today.isoformat()},
                'ended_at', 'Zaznacz „Zakończ etap i przekaż tekst dalej”.'),
            'przekazanie bez daty rozpoczęcia': (
                {'started_at': '', 'ended_at': self.today.isoformat(), 'finish': 'on'},
                'started_at', 'Zakończenie etapu wymaga obu dat.'),
        }
        for label, (data, field, message) in cases.items():
            with self.subTest(label):
                response = self.post(**data)
                self.assertEqual(response.status_code, 200)
                self.assertFormError(response.context['form'], field, message)
                self.assert_unchanged()

    def test_completed_stage_form_offers_only_date_correction(self):
        S.objects.filter(pk=self.stage.pk).update(ended_at=self.today, is_completed=True)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('finish', response.context['form'].fields)
        self.assertNotIn('next_stage', response.context['form'].fields)

    def test_future_completion_is_refused_and_rolls_back_new_start(self):
        """Zakończenie z przyszłą datą: komunikat o dacie (a nie ogólny o uprawnieniach), bez zmian w danych."""
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        cases = {'start and end tomorrow': (tomorrow, tomorrow), 'only end tomorrow': (self.today.isoformat(), tomorrow)}
        for case, (started_at, ended_at) in cases.items():
            with self.subTest(case):
                response = self.post(started_at=started_at, ended_at=ended_at, finish='on')
                self.assertEqual(response.status_code, 200)
                self.assertFormError(response.context['form'], None, 'Nie można zakończyć pracy z przyszłą datą.')
                self.assert_unchanged()

    def test_concurrent_changes_are_reported_on_admin_forms(self):
        """Rekord usunięty lub chroniony w międzyczasie: formularz pokazuje komunikat zamiast błędu serwera."""
        stage_correct = reverse('admin:workflow_stage_correct', args=[self.stage.pk])
        assignment_correct = reverse('admin:workflow_assignment_correct', args=[self.assignment.pk])
        version = version_of(self.text)
        cases = {
            'daty – etap usunięty': (
                'workflow.admin_stage_dates.set_stage_dates', S.DoesNotExist, self.url,
                {'started_at': self.start.isoformat(), 'version': version},
                'Rekord został w międzyczasie usunięty lub zmieniony. Odśwież stronę.'),
            'korekta etapu – etap usunięty': (
                'workflow.admin_stage_edit.edit_stage', S.DoesNotExist, stage_correct,
                {'action': 'reopen', 'version': version, 'confirm': 'on'},
                'Rekord został w międzyczasie usunięty lub zmieniony. Odśwież stronę.'),
            'korekta przypisania – przypisanie usunięte': (
                'workflow.admin_assignment_edit.correct_assignment', A.DoesNotExist, assignment_correct,
                {'action': 'clear', 'version': version, 'confirm': 'on'},
                'Rekord został w międzyczasie usunięty lub zmieniony. Odśwież stronę.'),
            'korekta przypisania – nowe chronione powiązanie': (
                'workflow.admin_assignment_edit.correct_assignment', ProtectedError('chronione', set()),
                assignment_correct, {'action': 'delete', 'version': version, 'confirm': 'on'},
                'Rekord ma chronione powiązania i nie został usunięty.'),
        }
        for label, (target, error, url, data, message) in cases.items():
            with self.subTest(label), mock.patch(target, side_effect=error):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 200)
                self.assertFormError(response.context['form'], None, message)
        self.assert_unchanged()
        self.assertTrue(A.objects.filter(pk=self.assignment.pk, assigned_to=self.member).exists())

    def test_clearing_start_date_keeps_performer(self):
        set_stage_dates(self.stage.pk, self.admin, version_of(self.text), started_at=None)
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.started_at)
        self.assertEqual(self.stage.assignment.assigned_to, self.member)

    def test_start_correction_requires_assigned_person(self):
        A.objects.filter(pk=self.assignment.pk).update(assigned_to=None)
        with self.assertRaisesMessage(ValidationError, 'Najpierw przypisz osobę do tego etapu.'):
            set_stage_dates(self.stage.pk, self.admin, version_of(self.text),
                            started_at=self.start + timedelta(days=1))
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)

    def test_editing_finish_requires_next_stage(self):
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        S.objects.filter(pk=self.stage.pk).update(ended_at=self.today, is_completed=True)
        editing = S.objects.create(text=self.text, stage_type='editing', assignment=editor,
                                   iteration=1, started_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'Wybierz kolejny etap redakcji.'):
            set_stage_dates(editing.pk, self.admin, version_of(self.text), started_at=self.today,
                            ended_at=self.today, finish=True)
        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

    def test_order_check_ignores_stage_deleted_in_meantime(self):
        removed = S(pk=self.stage.pk + 1000, text=self.text, stage_type='second_proofreading')
        self.assertIsNone(preserve_stage_order(removed, self.today, self.today))


class ManualStatusGapTests(StatusAssignmentFixtures):
    """Ręczna zmiana statusu: odmowy oraz statusy bez roli wykonawcy."""

    def test_refusals_change_nothing(self):
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today,
                         ended_at=self.today, is_completed=True,
                         assignment=A.objects.create(text=self.text, role='editor', assigned_to=self.member))
        cases = (
            ('bez uprawnień', lambda: set_admin_status(self.text.pk, 'editing', self.member, version_of(self.text)),
             PermissionDenied, ''),
            ('etap tylko z importu', lambda: self.status('editing_review'), ValidationError, 'Nieprawidłowy etap'),
            ('nieaktualny formularz', lambda: set_admin_status(self.text.pk, 'editing', self.admin,
                                                               version_of(self.text) - 1),
             ValidationError, 'Dane zmieniły się. Odśwież formularz.'),
        )
        for label, operation, error, message in cases:
            with self.subTest(label), self.assertRaisesMessage(error, message):
                operation()
        repeat_stages(self.text, ['editing'], self.admin)
        with self.assertRaisesMessage(ValidationError, 'aktywną kolejkę powtórzeń'):
            self.status('first_proofreading')
        self.assertFalse(S.objects.filter(text=self.text, stage_type='first_proofreading').exists())

    def test_statuses_without_performer_role_create_no_assignment(self):
        for kind in ('ready_for_editing', 'ready'):
            with self.subTest(kind=kind):
                stage = self.status(kind)
                self.assertEqual(stage.stage_type, kind)
                self.assertIsNone(stage.assignment_id)
                self.assertEqual(stage.execution_number, 1)
                self.assertFalse(A.objects.filter(text=self.text).exists())
                self.assertEqual(list(S.objects.current_cycle().filter(text=self.text)
                                      .values_list('stage_type', flat=True)), [kind])


class AddStageWithoutPerformerTests(EditingInProgressFixtures):
    """Dodanie brakującego etapu bez wskazania wykonawcy zostawia rolę wolną do przejęcia."""

    def test_first_verification_reservation_without_performer(self):
        stage = add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind='first_verification')
        self.assertEqual(stage.stage_type, 'first_verification')
        self.assertIsNone(stage.started_at)
        self.assertFalse(A.objects.filter(text=self.text, role='verifier_1', assigned_to__isnull=False).exists())

    def test_missing_open_stage_without_performer_becomes_status(self):
        self.close_editing()
        stage = add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind='first_proofreading')
        self.assertEqual(stage.stage_type, 'first_proofreading')
        self.assertIsNone(stage.started_at)
        self.assertIsNone(stage.assignment.assigned_to_id)


class ReservationRepairGapTests(EditingInProgressFixtures):
    """Przywrócenie rezerwacji W1, gdy przydział jest nadal bieżący, a tekst ma zamkniętą, niezwiązaną kolejkę."""

    def test_current_assignment_and_closed_unrelated_repetition_allow_repair(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.other)
        with importing():
            first = S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment,
                                     is_current=False, is_released=False, imported_completed=True,
                                     is_completed=True)
        WorkflowRepetition.objects.create(text=self.text, completed_at=timezone.now(),
                                          previous_stage_ids=[self.editing.pk])
        report = restore_first_verification_reservation(first.pk, apply=True)
        self.assertTrue(report['applied'])
        self.assertTrue(report['before']['assignment_current'])
        first.refresh_from_db()
        self.assertFalse(first.is_completed)
        self.assertTrue(first.is_current and first.is_released)
        self.assertEqual(first.assignment_id, assignment.pk)


class AssignmentNotesAdminTests(StatusAssignmentFixtures):
    """W adminie przypisania można zmienić tylko notatkę – także pustej roli i dawnej roli z importu."""

    def save_notes(self, assignment, notes):
        url = reverse('admin:workflow_workflowroleassignment_change', args=[assignment.pk])
        self.client.force_login(self.admin)
        page = self.client.get(url)
        response = self.client.post(url, {'notes': notes, '_edit_version': form_value(page), '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None)
                         and response.context.get('errors'))
        assignment.refresh_from_db()
        return assignment

    def test_notes_are_saved_without_changing_performer(self):
        with importing():
            imported = A.objects.create(text=self.text, role='editing_reviewer', assigned_to=self.member,
                                        is_current=False)
        empty = A.objects.create(text=self.text, role='proofreader_1')
        for assignment, performer in ((empty, None), (imported, self.member.pk)):
            with self.subTest(role=assignment.role):
                assigned_at = assignment.assigned_at
                saved = self.save_notes(assignment, 'Uwaga redakcji')
                self.assertEqual(saved.notes, 'Uwaga redakcji')
                self.assertEqual(saved.assigned_to_id, performer)
                self.assertEqual(saved.assigned_at, assigned_at)


class ArchiveLabelTests(TestCase):
    """Teksty z archiwum: status bez roli zachowuje zwykłą nazwę etapu."""

    def test_imported_status_without_role_uses_stage_name(self):
        text = Text(title='Archiwalny', import_source=SOURCE, import_source_row=3)
        for kind, expected in (('editing', 'Archiwalny: Redaktor'), ('ready', 'Archiwalny: Gotowe – przebieg 1')):
            with self.subTest(kind=kind):
                stage = S(text=text, stage_type=kind, imported_completed=True, execution_number=1)
                self.assertEqual(str(stage), expected)
