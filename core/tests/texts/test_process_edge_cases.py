"""Reguły serwisu procesu tekstu (core.services.texts) przy nietypowym stanie etapów:
kolejność rozpoczynania pracy, wejście w proces, rezerwacje stylowania i kolejnych rund redakcji,
przywrócenie tekstu wycofanego ręcznym statusem."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.edit_versions import version_of
from core.services.texts import change_scheduled_stage, restore_withdrawn_text, start_assigned_stage
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from workflow.admin_add_stage import add_missing_stage
from workflow.admin_status import set_admin_status
from workflow.models import WorkflowRoleAssignment, WorkflowStage

S = WorkflowStage
A = WorkflowRoleAssignment
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


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


class StylingAndWithdrawalTests(WorkflowTestDataMixin, TestCase):
    """Stylowanie zostaje w rękach superusera; tekst wycofany ręcznym statusem da się przywrócić."""

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
