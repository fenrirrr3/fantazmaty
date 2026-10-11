"""Reguły serwisu procesu tekstu (core.services.texts): rozpoczęcie przypisanego etapu, zmiana
i odwołanie rezerwacji, cofnięcie przydziału oraz wycofanie i przywrócenie tekstu – odmowy
i przypadki brzegowe (pełne przejścia procesu testuje obszar workflow)."""
from datetime import timedelta
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase

from core.services.texts import (change_scheduled_stage, release_blocker, restore_withdrawn_text,
                                 start_assigned_stage, withdraw_text)
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment, WorkflowStage
from workflow.services import claim_ready_for_editing, claim_stage, send_to_first_verification

StageType = WorkflowStage.StageType


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
