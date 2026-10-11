"""Redakcja i weryfikacje: przejmowanie tekstu, przekazania do weryfikacji, uprawnienia i daty rozpoczęcia."""
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import text_detail_context
from core.services.texts import start_assigned_stage
from core.workflow_tokens import make_token
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from people.models import Person
from texts.models import Text
from workflow.admin_add_stage import add_missing_stage
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.repetitions import repeat_stages
from workflow.services import (
    claim_ready_for_editing,
    claim_stage,
    complete_stage,
    current_assignment_queryset,
    current_stage_queryset,
    editing_follows_first_verification,
    finish_editing_to_coordinator,
    resume_editing,
    send_text_to_author,
    send_to_first_verification,
    send_to_second_verification,
    start_first_verification,
    user_can_complete_stage,
    validate_assignment_start_date,
)

from .support import importing

S = WorkflowStage
A = WorkflowRoleAssignment
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


class AssignmentStartDateTests(SimpleTestCase):
    """Dopuszczalna data rozpoczęcia przejmowanej pracy: od dziś do 14 dni naprzód."""

    def test_assignment_start_date_accepts_supported_boundaries(self):
        today = timezone.localdate()

        self.assertEqual(validate_assignment_start_date(today), today)
        self.assertEqual(
            validate_assignment_start_date(today + timedelta(days=14)),
            today + timedelta(days=14),
        )

    def test_assignment_start_date_rejects_out_of_range_dates(self):
        today = timezone.localdate()

        for value in (
            today - timedelta(days=1),
            today + timedelta(days=15),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_assignment_start_date(value)

    def test_assignment_start_date_rejects_invalid_types(self):
        for value in (None, "2026-01-01", datetime(2026, 1, 1)):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_assignment_start_date(value)


class EditingAndVerificationFlowTests(WorkflowTestDataMixin, TestCase):
    """Pełna ścieżka redakcji i weryfikacji przez usługi procesu."""

    def test_claiming_editing_assigns_editor_and_prepares_verification(self):
        editing = self.begin_editing()

        self.assertEqual(editing.stage_type, StageType.EDITING)
        self.assertEqual(editing.started_at, self.today)
        self.assertFalse(editing.is_completed)

        self.assertFalse(
            WorkflowStage.objects.filter(pk=self.initial_stage.pk).exists()
        )
        self.assertTrue(
            current_assignment_queryset(self.text).filter(
                role=Role.EDITOR,
                assigned_to=self.editor,
            ).exists()
        )
        self.assertIsNone(
            self.stage(StageType.FIRST_VERIFICATION).started_at
        )

    def test_first_verifier_reserves_stage_without_starting_it(self):
        self.begin_editing()

        verification = claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )

        self.assertIsNone(verification.started_at)

        with self.assertRaises(ValidationError):
            start_first_verification(
                self.text,
                self.verifier_1,
                started_at=self.today,
            )

    def test_first_verification_can_wait_for_assignment_after_handover(self):
        text = self.text
        claim_ready_for_editing(text, self.editor)
        pending = send_to_first_verification(text, self.editor)
        self.assertIsNone(pending.started_at)
        self.assertFalse(pending.is_completed)
        self.assertFalse(text.workflow_role_assignments.filter(role="verifier_1").exists())

    def test_same_person_cannot_perform_first_and_second_verification(self):
        self.finish_first_verification()
        resume_editing(self.text, self.editor)
        send_to_second_verification(self.text, self.editor)

        with self.assertRaises(ValidationError):
            claim_stage(
                self.text,
                StageType.SECOND_VERIFICATION,
                self.verifier_1,
            )

        self.assertFalse(
            current_assignment_queryset(self.text).filter(
                role=Role.VERIFIER_2,
            ).exists()
        )
        self.assertIsNone(
            self.stage(StageType.SECOND_VERIFICATION).started_at
        )

    def test_unavailable_stage_cannot_be_created_by_claiming_it(self):
        with self.assertRaises(ValidationError):
            claim_stage(
                self.text,
                StageType.FOURTH_PROOFREADING,
                self.proofreader,
            )

        self.assertFalse(
            current_stage_queryset(self.text).filter(
                stage_type=StageType.FOURTH_PROOFREADING,
            ).exists()
        )
        self.assertFalse(
            current_assignment_queryset(self.text).exists()
        )

    def test_editing_cannot_resume_while_verification_waits_to_start(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )
        send_to_first_verification(self.text, self.editor)

        with self.assertRaises(ValidationError):
            resume_editing(self.text, self.editor)

        self.assertFalse(
            current_stage_queryset(self.text).filter(
                stage_type=StageType.EDITING,
                is_completed=False,
            ).exists()
        )

    def test_author_round_trip_preserves_previous_editing_iteration(self):
        self.finish_first_verification()
        editing = resume_editing(self.text, self.editor)

        author_stage = send_text_to_author(
            self.text,
            self.editor,
            started_at=self.today,
        )
        editing.refresh_from_db()

        self.assertTrue(editing.is_completed)
        self.assertEqual(author_stage.started_at, self.today)

        resumed = resume_editing(self.text, self.editor)
        author_stage.refresh_from_db()

        self.assertTrue(author_stage.is_completed)
        self.assertGreater(resumed.iteration, editing.iteration)
        self.assertNotEqual(resumed.pk, editing.pk)

    def test_full_workflow_reaches_ready(self):
        self.finish_second_verification()

        resume_editing(self.text, self.editor)
        finish_editing_to_coordinator(self.text, self.editor)

        steps = (
            (StageType.EDITING_CONTROL, self.coordinator),
            (StageType.FIRST_PROOFREADING, self.proofreader),
            (StageType.SECOND_PROOFREADING, self.proofreading_coordinator),
            (StageType.THIRD_VERIFICATION, self.verifier_1),
            (StageType.COORDINATOR_CONTROL, self.coordinator),
        )

        for stage_type, user in steps:
            with self.subTest(stage=stage_type):
                stage = claim_stage(self.text, stage_type, user)
                complete_stage(stage, user, self.today, send_to_proofreading=True if stage_type == StageType.EDITING_CONTROL else None)

        editor_control = self.stage(StageType.EDITOR_CONTROL)

        self.assertIsNone(editor_control.started_at)
        from core.services.texts import start_assigned_stage
        start_assigned_stage(user=self.editor, stage_id=editor_control.pk, started_at=self.today)
        editor_control.refresh_from_db()
        self.assertFalse(
            user_can_complete_stage(editor_control, self.coordinator)
        )

        complete_stage(editor_control, self.editor, self.today)

        for stage_type, user in (
            (StageType.THIRD_PROOFREADING, self.other_proofreader),
            (StageType.FOURTH_PROOFREADING, self.proofreading_coordinator),
            (StageType.STYLING, self.superuser),
        ):
            stage = claim_stage(self.text, stage_type, user)
            complete_stage(stage, user, self.today)

        ready = self.stage(StageType.READY)

        self.assertEqual(ready.started_at, self.today)
        self.assertFalse(
            current_stage_queryset(self.text)
            .filter(is_completed=False)
            .exclude(stage_type=StageType.READY)
            .exists()
        )

    def test_editing_cannot_resume_after_handover_to_coordinator(self):
        self.finish_second_verification()
        resume_editing(self.text, self.editor)
        finish_editing_to_coordinator(self.text, self.editor)

        with self.assertRaises(ValidationError):
            resume_editing(self.text, self.editor)

    def test_generic_completion_cannot_bypass_editing_transitions(self):
        editing = self.begin_editing()

        with self.assertRaises(ValidationError):
            complete_stage(editing, self.editor, self.today)

        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

    def test_repeating_completion_does_not_create_another_next_stage(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )
        send_to_first_verification(self.text, self.editor)
        verification = start_first_verification(
            self.text,
            self.verifier_1,
        )
        complete_stage(verification, self.verifier_1, self.today)

        count_before = current_stage_queryset(self.text).count()

        with self.assertRaises(ValidationError):
            complete_stage(verification, self.verifier_1, self.today)

        self.assertEqual(
            current_stage_queryset(self.text).count(),
            count_before,
        )

    def test_imported_undated_first_verification_allows_second(self):
        self.begin_editing()
        self.stage("first_verification").delete()
        with importing():
            first = S.objects.create(
                text=self.text,
                stage_type="first_verification",
                is_completed=True,
                imported_completed=True,
                is_current=False,
            )
        self.assertTrue(
            text_detail_context(user=self.editor, text=self.text)["can_send_to_second_verification"]
        )
        next_stage = send_to_second_verification(self.text, self.editor, self.today)
        self.assertEqual(next_stage.stage_type, "second_verification")
        first.refresh_from_db()
        self.assertIsNone(first.ended_at)

    def test_live_first_verification_still_requires_resumption(self):
        self.begin_editing()
        first = self.stage("first_verification")
        S.objects.filter(pk=first.pk).update(
            started_at=self.today, ended_at=self.today, is_completed=True
        )
        self.assertFalse(
            text_detail_context(user=self.editor, text=self.text)["can_send_to_second_verification"]
        )
        with self.assertRaises(ValidationError):
            send_to_second_verification(self.text, self.editor, self.today)


class WorkflowPermissionTests(WorkflowTestDataMixin, TestCase):
    """Kto może przejąć, przekazać i rozpocząć pracę."""

    def test_wrong_role_cannot_claim_editing(self):
        with self.assertRaises(PermissionDenied):
            claim_ready_for_editing(self.text, self.proofreader)

        self.assertTrue(
            WorkflowStage.objects.filter(pk=self.initial_stage.pk).exists()
        )

    def test_former_member_cannot_claim_even_with_django_group(self):
        Person.objects.filter(user=self.editor).update(is_active=False)

        self.assertTrue(
            self.editor.groups.filter(name="Redaktor").exists()
        )

        with self.assertRaises(PermissionDenied):
            claim_ready_for_editing(self.text, self.editor)

    def test_inactive_account_cannot_claim(self):
        self.editor.is_active = False
        self.editor.save(update_fields=["is_active"])

        with self.assertRaises(PermissionDenied):
            claim_ready_for_editing(self.text, self.editor)

    def test_unassigned_editor_cannot_handover_another_persons_text(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )

        with self.assertRaises(PermissionDenied):
            send_to_first_verification(self.text, self.other_editor)

    def test_unassigned_verifier_cannot_start_reserved_verification(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )
        send_to_first_verification(self.text, self.editor)

        with self.assertRaises(PermissionDenied):
            start_first_verification(self.text, self.verifier_2)

    def test_coordinator_cannot_restart_workflow(self):
        with self.assertRaises(PermissionDenied):
            repeat_stages(
                self.text,
                [StageType.FIRST_PROOFREADING],
                self.coordinator,
            )

    def test_existing_assignment_can_start_without_current_role(self):
        """Regresja: osoba z istniejącym przydziałem nie mogła rozpocząć etapu po utracie roli."""
        text = Text.objects.create(title="Korekta", length=1)
        assignment = A.objects.create(text=text, role="proofreader_1", assigned_to=self.proofreader)
        stage = S.objects.create(text=text, stage_type="first_proofreading", assignment=assignment)
        self.proofreader.person_profile.roles.clear()
        self.proofreader.groups.clear()
        start_assigned_stage(user=self.proofreader, stage_id=stage.pk, started_at=self.today)
        stage.refresh_from_db()
        self.assertEqual(stage.started_at, self.today)


class FirstVerificationStartTests(WorkflowTestDataMixin, TestCase):
    """Pierwsza weryfikacja nie może ruszyć, póki trwa praca redakcyjna (oba wejścia: usługa i strona)."""

    def pending_first(self, editorial_type):
        self.initial_stage.is_current = False
        self.initial_stage.save(update_fields=['is_current'])
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        editorial = S.objects.create(text=self.text, stage_type=editorial_type, assignment=editor)
        verifier = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.verifier_1)
        first = S.objects.create(text=self.text, stage_type='first_verification', assignment=verifier)
        return editorial, first

    def assert_start_blocked(self, kind):
        editorial, first = self.pending_first(kind)
        for start in (
            lambda: start_first_verification(self.text, self.verifier_1, self.today),
            lambda: start_assigned_stage(user=self.verifier_1, stage_id=first.pk,
                                         started_at=self.today),
        ):
            with self.assertRaises(ValidationError):
                start()
            first.refresh_from_db()
            editorial.refresh_from_db()
            self.assertIsNone(first.started_at)
            self.assertFalse(editorial.is_completed)

    def test_both_entry_points_block_pending_editing(self):
        self.assert_start_blocked('editing')

    def test_both_entry_points_block_pending_author_editing(self):
        self.assert_start_blocked('author_editing')

    def test_direct_post_cannot_bypass_pending_editorial_work(self):
        editorial, first = self.pending_first('editing')
        self.client.force_login(self.verifier_1)
        response = self.client.post(reverse('core:start_first_verification_stage', args=[self.text.pk]),
                                    {'workflow_token': make_token(self.text, self.verifier_1)})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        editorial.refresh_from_db()
        self.assertIsNone(first.started_at)
        self.assertFalse(editorial.is_completed)
        self.assertTrue(any('Tekst nadal znajduje' in str(message)
                            for message in response.wsgi_request._messages))

    def test_completed_editorial_work_allows_first_verification(self):
        editorial, first = self.pending_first('editing')
        editorial.started_at = editorial.ended_at = timezone.localdate()
        editorial.is_completed = True
        editorial.save()
        started = start_first_verification(self.text, self.verifier_1, self.today)
        self.assertEqual(started.pk, first.pk)
        self.assertEqual(started.started_at, self.today)

    def assert_archived_verification_cannot_be_added_again(self, kind, role):
        assignment = A.objects.create(text=self.text, role=role, assigned_to=self.verifier_1,
                                      is_current=False)
        completed = S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                                     started_at=self.today, ended_at=self.today,
                                     is_completed=True, is_current=False)
        for values in ({'historical': True}, {'started_at': self.today, 'ended_at': self.today}):
            with self.assertRaisesMessage(ValidationError, 'już zakończona'):
                add_missing_stage(self.text.pk, self.superuser, version_of(self.text),
                                  kind=kind, performer=self.verifier_2, **values)
            self.assertEqual(S.objects.filter(text=self.text, stage_type=kind).count(), 1)
            self.assertEqual(A.objects.filter(text=self.text, role=role).count(), 1)
            completed.refresh_from_db()
            self.assertEqual(completed.assignment_id, assignment.pk)

    def test_archived_first_is_not_a_missing_stage(self):
        self.assert_archived_verification_cannot_be_added_again('first_verification', 'verifier_1')

    def test_archived_second_is_not_a_missing_stage(self):
        self.assert_archived_verification_cannot_be_added_again('second_verification', 'verifier_2')


class EditingAfterFirstVerificationTests(TestCase):
    """Czy trwająca redakcja jest już przebiegiem po pierwszej weryfikacji (warunek przekazania do drugiej)."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.yesterday = cls.today - timedelta(days=1)

    def editing_with_first(self, **first):
        text = Text.objects.create(title='Tekst', length=100)
        editing = S.objects.create(text=text, stage_type='editing', started_at=self.today)
        if first:
            with importing():
                S.objects.create(text=text, stage_type='first_verification', is_completed=True,
                                 is_current=False, **first)
        return text, editing

    def test_order_of_editing_and_completed_first_verification(self):
        for first, expected in (({}, False),
                                ({'started_at': self.yesterday, 'ended_at': self.yesterday}, True),
                                # Same day: the W1 record created after editing began does not approve it.
                                ({'started_at': self.today, 'ended_at': self.today}, False),
                                ({'imported_completed': True}, True)):
            with self.subTest(first=first):
                text, editing = self.editing_with_first(**first)
                self.assertIs(editing_follows_first_verification(text, editing), expected)
