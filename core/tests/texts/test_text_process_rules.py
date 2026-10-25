"""Reguły serwisu procesu tekstu (core.services.texts): rozpoczęcie przypisanego etapu, zmiana
i odwołanie rezerwacji, cofnięcie przydziału oraz wycofanie i przywrócenie tekstu – odmowy
i przypadki brzegowe: kolejność rozpoczynania pracy, stylowanie superusera, komunikat odmowy z usługi
(pełne przejścia procesu testuje obszar workflow)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.edit_versions import version_of

from core.services.texts import (change_scheduled_stage, release_blocker, restore_withdrawn_text,
                                 start_assigned_stage, withdraw_text)
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from workflow.admin_add_stage import add_missing_stage
from workflow.admin_status import set_admin_status
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment, WorkflowStage
from workflow.services import claim_ready_for_editing, claim_stage, send_to_first_verification

S = WorkflowStage
A = WorkflowRoleAssignment
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


class ReleaseBlockerTests(SimpleTestCase):
    def stage(self, stage_type, started_at=None, repetition_id=None, is_completed=False):
        return SimpleNamespace(stage_type=stage_type, started_at=started_at, repetition_id=repetition_id,
                               is_completed=is_completed)

    def test_reasons_for_refusing_to_give_back_work(self):
        day = SimpleNamespace()
        editing = self.stage(StageType.EDITING, day)
        cases = [
            (self.stage(StageType.READY_FOR_EDITING), [], 'użyj przekazania pracy'),
            (self.stage(StageType.FIRST_PROOFREADING, day, repetition_id=1), [], 'kolejki powtórzeń'),
            (self.stage(StageType.FIRST_PROOFREADING), [], 'nie ma rezerwacji'),
            (editing, [editing, self.stage(StageType.FIRST_VERIFICATION, day)], 'Weryfikacja już trwa'),
        ]
        for stage, stages, reason in cases:
            with self.subTest(stage=stage.stage_type):
                self.assertIn(reason, release_blocker(stage, stages))
        for stage, stages in ((self.stage(StageType.FIRST_VERIFICATION), []),
                              (editing, [editing, self.stage(StageType.FIRST_VERIFICATION, day, is_completed=True)])):
            self.assertIsNone(release_blocker(stage, stages))


class TextProcessServiceTests(WorkflowTestDataMixin, TestCase):
    def start(self, user, stage, started_at=None, **kwargs):
        return start_assigned_stage(user=user, stage_id=stage.pk, started_at=started_at or self.today, **kwargs)

    def test_start_refuses_started_finished_foreign_and_unassigned_stages(self):
        editing = self.begin_editing()
        first = self.stage(StageType.FIRST_VERIFICATION)
        with self.assertRaisesMessage(ValidationError, 'już zapisana'):
            self.start(self.editor, editing)
        with self.assertRaisesMessage(ValidationError, 'Najpierw przypisz osobę'):
            self.start(self.coordinator, first)
        claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
        with self.assertRaises(PermissionDenied):
            self.start(self.verifier_2, first)
        with self.assertRaises(PermissionDenied):
            self.start(self.editor, first, allow_past=True)
        WorkflowStage.objects.filter(pk=first.pk).update(is_current=False)
        with self.assertRaisesMessage(ValidationError, 'bieżącego cyklu'):
            self.start(self.verifier_1, first)
        WorkflowStage.objects.filter(pk=first.pk).update(is_current=True, is_completed=True, started_at=self.today,
                                                              ended_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'już zakończony'):
            self.start(self.verifier_1, first)

    def test_start_cannot_precede_end_of_earlier_work(self):
        editing = self.begin_editing()
        claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
        send_to_first_verification(self.text, self.editor, ended_at=self.today)
        WorkflowStage.objects.filter(pk=editing.pk).update(ended_at=self.today + timedelta(days=2))
        with self.assertRaisesMessage(ValidationError, 'nie może poprzedzać zakończenia'):
            self.start(self.verifier_1, self.stage(StageType.FIRST_VERIFICATION))

    def test_only_superuser_starts_styling(self):
        styling = WorkflowStage.objects.create(text=self.text, stage_type=StageType.STYLING)
        with self.assertRaisesMessage(PermissionDenied, 'Stylowanie'):
            self.start(self.coordinator, styling)

    def test_future_booking_can_be_moved_or_cancelled_only_by_its_owner(self):
        editing = claim_ready_for_editing(self.text, self.editor, started_at=self.today + timedelta(days=3))
        later = self.today + timedelta(days=5)
        with self.assertRaises(PermissionDenied):
            change_scheduled_stage(user=self.other_editor, stage_id=editing.pk, started_at=later)
        with self.assertRaises(ValidationError):
            change_scheduled_stage(user=self.editor, stage_id=editing.pk, started_at=self.today + timedelta(days=20))
        change_scheduled_stage(user=self.editor, stage_id=editing.pk, started_at=later)
        editing.refresh_from_db()
        self.assertEqual(editing.started_at, later)
        change_scheduled_stage(user=self.editor, stage_id=editing.pk, cancel=True)
        editing.refresh_from_db()
        self.assertEqual((editing.stage_type, editing.started_at), (StageType.READY_FOR_EDITING, None))
        self.assertFalse(WorkflowRoleAssignment.objects.filter(text=self.text, role='editor',
                                                               assigned_to__isnull=False).exists())

    def test_started_or_finished_work_and_missing_performer_cannot_be_rescheduled(self):
        editing = self.begin_editing()
        with self.assertRaisesMessage(ValidationError, 'wyłącznie rezerwację z przyszłą datą'):
            change_scheduled_stage(user=self.editor, stage_id=editing.pk, started_at=self.today + timedelta(days=1))
        first = self.stage(StageType.FIRST_VERIFICATION)
        WorkflowStage.objects.filter(pk=first.pk).update(started_at=self.today + timedelta(days=2))
        with self.assertRaisesMessage(ValidationError, 'nie ma przypisanego wykonawcy'):
            change_scheduled_stage(user=self.coordinator, stage_id=first.pk, started_at=self.today)
        WorkflowStage.objects.filter(pk=editing.pk).update(is_completed=True, ended_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'już zakończony'):
            change_scheduled_stage(user=self.editor, stage_id=editing.pk, cancel=True)

    def test_withdrawal_is_for_coordinators_repeatable_and_not_for_ready_texts(self):
        with self.assertRaises(PermissionDenied):
            withdraw_text(user=self.editor, text_id=self.text.pk)
        with self.assertRaisesMessage(ValidationError, 'nie jest wycofany'):
            restore_withdrawn_text(user=self.superuser, text_id=self.text.pk)
        stage = withdraw_text(user=self.coordinator, text_id=self.text.pk)
        self.assertEqual(withdraw_text(user=self.coordinator, text_id=self.text.pk), stage)
        with self.assertRaises(ValidationError):
            change_scheduled_stage(user=self.coordinator, stage_id=self.initial_stage.pk, cancel=True)
        restore_withdrawn_text(user=self.superuser, text_id=self.text.pk)
        WorkflowStage.objects.filter(pk=self.initial_stage.pk).update(stage_type=StageType.READY)
        with self.assertRaisesMessage(ValidationError, 'Gotowego tekstu nie można wycofać'):
            withdraw_text(user=self.coordinator, text_id=self.text.pk)

    def test_withdrawal_cancels_open_repetition_and_then_cannot_be_undone_in_one_step(self):
        run = WorkflowRepetition.objects.create(text=self.text, created_by=self.coordinator,
                                                selected_stages=[StageType.FIRST_PROOFREADING])
        pending = WorkflowStage.objects.create(text=self.text, stage_type=StageType.FIRST_PROOFREADING,
                                               repetition=run)
        withdraw_text(user=self.coordinator, text_id=self.text.pk)
        run.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual((run.canceled_by, run.cancellation_reason), (self.coordinator, 'Wycofano tekst.'))
        self.assertIsNotNone(run.canceled_at)
        self.assertEqual((pending.is_current, pending.is_released), (False, False))
        with self.assertRaisesMessage(ValidationError, 'kolejkę powtórzeń'):
            restore_withdrawn_text(user=self.superuser, text_id=self.text.pk)

    def test_coordinator_cannot_move_styling_booking(self):
        S.objects.filter(text=self.text).delete()
        role = A.objects.create(text=self.text, role=Role.STYLING, assigned_to=self.superuser)
        booked = self.today + timedelta(days=2)
        styling = S.objects.create(text=self.text, stage_type=StageType.STYLING, assignment=role,
                                   started_at=booked)
        with self.assertRaisesMessage(PermissionDenied, 'Stylowaniem zarządza superuser'):
            change_scheduled_stage(user=self.coordinator, stage_id=styling.pk, cancel=True)
        styling.refresh_from_db()
        self.assertEqual(styling.started_at, booked)

    def test_styling_cannot_be_added_for_team_member(self):
        with self.assertRaisesMessage(ValidationError, 'Stylowanie można przypisać tylko superuserowi'):
            add_missing_stage(self.text.pk, self.superuser, version_of(self.text), kind=StageType.STYLING,
                              performer=self.coordinator, started_at=self.today)
        self.assertFalse(S.objects.filter(text=self.text, stage_type=StageType.STYLING).exists())

    def test_text_withdrawn_by_manual_status_can_be_restored(self):
        withdrawn = set_admin_status(self.text.pk, StageType.WITHDRAWN, self.superuser, version_of(self.text))
        self.assertIsNone(withdrawn.started_at)
        restore_withdrawn_text(user=self.superuser, text_id=self.text.pk)
        self.assertFalse(S.objects.filter(pk=withdrawn.pk).exists())


class StartOrderTests(WorkflowTestDataMixin, TestCase):
    """Rozpoczęcie przypisanego etapu musi zgadzać się z kolejnością procesu."""

    def setUp(self):
        super().setUp()
        S.objects.filter(text=self.text).delete()
        self.editor_role = A.objects.create(text=self.text, role=Role.EDITOR, assigned_to=self.editor)

    def done(self, kind, **extra):
        return S.objects.create(text=self.text, stage_type=kind, started_at=self.today, ended_at=self.today,
                                is_completed=True, **extra)

    def pending(self, kind, **extra):
        return S.objects.create(text=self.text, stage_type=kind, **extra)

    def start(self, stage, user=None):
        return start_assigned_stage(user=user or self.editor, stage_id=stage.pk, started_at=self.today)

    def test_refusals_keep_stage_unstarted(self):
        def editing_waits_for_verification():
            self.done(StageType.EDITING)
            self.pending(StageType.FIRST_VERIFICATION)
            return self.pending(StageType.EDITING, iteration=2), self.editor

        def editing_after_later_work():
            self.done(StageType.EDITING)
            self.done(StageType.FIRST_PROOFREADING)
            return self.pending(StageType.EDITING, iteration=2), self.editor

        def ready_for_editing_with_other_work():
            self.pending(StageType.FIRST_PROOFREADING)
            return self.pending(StageType.READY_FOR_EDITING), self.editor

        def proofreading_before_control():
            A.objects.create(text=self.text, role=Role.PROOFREADER_1, assigned_to=self.proofreader)
            self.pending(StageType.EDITING_CONTROL)
            return self.pending(StageType.FIRST_PROOFREADING), self.proofreader

        def author_without_handoff():
            return self.pending(StageType.AUTHOR_EDITING), self.editor

        cases = {
            'redakcja przy oczekującej W1': (editing_waits_for_verification, 'podczas oczekiwania na weryfikację'),
            'redakcja po dalszych etapach': (editing_after_later_work, 'przeszedł już do dalszych etapów'),
            '„Do redakcji” przy innej pracy': (ready_for_editing_with_other_work, 'rozpoczęty lub przygotowany proces'),
            'korekta przed kontrolą redakcji': (proofreading_before_control, 'Najpierw zakończ wcześniejszy etap'),
            'praca autora bez przekazania': (author_without_handoff, 'przez przekazanie tekstu autorowi'),
        }
        for label, (prepare, message) in cases.items():
            with self.subTest(label):
                S.objects.filter(text=self.text).delete()
                A.objects.filter(text=self.text).exclude(pk=self.editor_role.pk).delete()
                stage, user = prepare()
                with self.assertRaisesMessage(ValidationError, message):
                    self.start(stage, user)
                stage.refresh_from_db()
                self.assertIsNone(stage.started_at)

    def test_reserved_ready_for_editing_keeps_existing_first_verification(self):
        ready = self.pending(StageType.READY_FOR_EDITING)
        reservation = self.pending(StageType.FIRST_VERIFICATION)
        started = self.start(ready)
        self.assertEqual((started.stage_type, started.started_at), (StageType.EDITING, self.today))
        self.assertEqual(list(S.objects.filter(text=self.text, stage_type=StageType.FIRST_VERIFICATION)
                              .values_list('pk', flat=True)), [reservation.pk])

    def test_starting_lone_editing_prepares_first_verification(self):
        editing = self.pending(StageType.EDITING)
        self.start(editing)
        editing.refresh_from_db()
        self.assertEqual(editing.started_at, self.today)
        verification = S.objects.get(text=self.text, stage_type=StageType.FIRST_VERIFICATION)
        self.assertIsNone(verification.started_at)
        self.assertFalse(verification.is_completed)


class LaterEditingRoundBookingTests(WorkflowTestDataMixin, TestCase):
    """Odwołanie kolejnej rundy redakcji, gdy redaktor ma już wcześniejszą pracę w tej roli."""

    def setUp(self):
        super().setUp()
        S.objects.filter(text=self.text).delete()
        role = A.objects.create(text=self.text, role=Role.EDITOR, assigned_to=self.editor)
        yesterday = self.today - timedelta(days=1)
        S.objects.create(text=self.text, stage_type=StageType.EDITING, assignment=role,
                         started_at=yesterday, ended_at=yesterday, is_completed=True)
        S.objects.create(text=self.text, stage_type=StageType.FIRST_VERIFICATION, started_at=yesterday,
                         ended_at=yesterday, is_completed=True)
        self.round = S.objects.create(text=self.text, stage_type=StageType.EDITING, iteration=2,
                                      assignment=role)

    def test_started_round_cannot_be_given_back(self):
        S.objects.filter(pk=self.round.pk).update(started_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'ma już wcześniejszą pracę w tej roli'):
            change_scheduled_stage(user=self.editor, stage_id=self.round.pk, cancel=True)
        self.round.refresh_from_db()
        self.assertEqual(self.round.started_at, self.today)

    def test_cancelled_future_round_keeps_editor(self):
        S.objects.filter(pk=self.round.pk).update(started_at=self.today + timedelta(days=2))
        change_scheduled_stage(user=self.editor, stage_id=self.round.pk, cancel=True)
        self.round.refresh_from_db()
        self.assertIsNone(self.round.started_at)
        self.assertEqual(self.round.stage_type, StageType.EDITING)
        self.assertEqual(self.round.assignment.assigned_to, self.editor)


class LateRefusalMessageTests(WorkflowTestDataMixin, TestCase):
    """Usługa ponownie sprawdza uprawnienia już po wejściu do widoku: odmowa bez treści daje ogólny komunikat
    i powrót do tekstu, bez zmian w danych."""

    def test_permission_refusal_from_service_is_shown_as_message(self):
        cases = {
            'withdraw': ('core:withdraw_text', self.text.pk, 'core.views.workflow.withdraw_text_service'),
            'restore': ('core:restore_withdrawn_text', self.text.pk,
                        'core.views.workflow.restore_withdrawn_text_service'),
            'skip fourth proofreading': ('core:skip_workflow_stage', self.initial_stage.pk,
                                         'workflow.services.skip_fourth_proofreading'),
        }
        self.client.force_login(self.superuser)
        for case, (name, pk, service) in cases.items():
            with self.subTest(case), patch('core.workflow_tokens.check_token'), \
                    patch(service, side_effect=PermissionDenied) as called:
                response = self.client.post(reverse(name, args=[pk]), follow=True)
                called.assert_called_once()
                self.assertIn('Nie masz uprawnień do tej operacji.', [str(m) for m in response.context['messages']])
        self.assertEqual(WorkflowStage.objects.filter(text=self.text).count(), 1)
