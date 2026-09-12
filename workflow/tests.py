from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    IntegrityError,
    close_old_connections,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from people.models import Person, Role as PersonRole
from texts.models import Text

from .models import WorkflowRoleAssignment, WorkflowStage
from .services import (
    claim_ready_for_editing,
    claim_stage,
    complete_stage,
    create_pending_stage,
    current_assignment_queryset,
    current_stage_queryset,
    finish_editing_to_coordinator,
    finish_stage_record,
    restart_workflow_from_stage,
    resume_editing,
    send_text_to_author,
    send_to_first_verification,
    send_to_second_verification,
    start_first_verification,
    user_can_complete_stage,
    validate_assignment_start_date,
)


User = get_user_model()
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


def create_member(username, role_name):
    user = User.objects.create_user(username=username)
    person = Person.objects.create(
        first_name=username,
        last_name="Testowy",
        email=f"{username}@example.com",
        user=user,
    )
    role, _ = PersonRole.objects.get_or_create(name=role_name)
    person.roles.add(role)

    group, _ = Group.objects.get_or_create(name=role_name)
    user.groups.add(group)

    return user


class WorkflowTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.editor = create_member("redaktor", "Redaktor")
        cls.other_editor = create_member("drugi_redaktor", "Redaktor")
        cls.verifier_1 = create_member("weryfikator_1", "Weryfikator")
        cls.verifier_2 = create_member("weryfikator_2", "Weryfikator")
        cls.proofreader = create_member("korektor", "Korektor")
        cls.coordinator = create_member("koordynator", "Koordynator")

        cls.superuser = User.objects.create_superuser(
            username="superadmin",
            email="superadmin@example.com",
            password="Testowe-haslo-123!",
        )

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.text = Text.objects.create(
            title="Tekst do testów workflow",
            length=25000,
        )
        self.initial_stage = WorkflowStage.objects.create(
            text=self.text,
            workflow_cycle=1,
            stage_type=StageType.READY_FOR_EDITING,
        )

    def stage(self, stage_type):
        return current_stage_queryset(self.text).get(
            stage_type=stage_type,
            is_completed=False,
        )

    def begin_editing(self):
        return claim_ready_for_editing(
            self.text,
            self.editor,
            started_at=self.today,
        )

    def finish_first_verification(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )
        send_to_first_verification(
            self.text,
            self.editor,
            ended_at=self.today,
        )
        stage = start_first_verification(
            self.text,
            self.verifier_1,
            started_at=self.today,
        )
        complete_stage(stage, self.verifier_1, self.today)

    def finish_second_verification(self):
        self.finish_first_verification()
        resume_editing(
            self.text,
            self.editor,
            started_at=self.today,
        )
        send_to_second_verification(
            self.text,
            self.editor,
            started_at=self.today,
        )
        stage = claim_stage(
            self.text,
            StageType.SECOND_VERIFICATION,
            self.verifier_2,
        )
        complete_stage(stage, self.verifier_2, self.today)


class WorkflowDateTests(TestCase):
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


class WorkflowProgressionTests(WorkflowTestDataMixin, TestCase):
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

    def test_first_verification_requires_assignment_before_handover(self):
        editing = self.begin_editing()

        with self.assertRaises(ValidationError):
            send_to_first_verification(self.text, self.editor)

        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

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
            (StageType.SECOND_PROOFREADING, self.proofreader),
            (StageType.THIRD_VERIFICATION, self.verifier_1),
            (StageType.COORDINATOR_CONTROL, self.coordinator),
        )

        for stage_type, user in steps:
            with self.subTest(stage=stage_type):
                stage = claim_stage(self.text, stage_type, user)
                complete_stage(stage, user, self.today)

        editor_control = self.stage(StageType.EDITOR_CONTROL)

        self.assertEqual(editor_control.started_at, self.today)
        self.assertFalse(
            user_can_complete_stage(editor_control, self.coordinator)
        )

        complete_stage(editor_control, self.editor, self.today)

        for stage_type, user in (
            (StageType.THIRD_PROOFREADING, self.proofreader),
            (StageType.FOURTH_PROOFREADING, self.proofreader),
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


class WorkflowPermissionTests(WorkflowTestDataMixin, TestCase):
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
            restart_workflow_from_stage(
                self.text,
                StageType.FIRST_PROOFREADING,
                self.coordinator,
            )


class WorkflowRestartTests(WorkflowTestDataMixin, TestCase):
    def test_restart_preserves_old_stages_and_assignments(self):
        editing = self.begin_editing()
        old_assignment = current_assignment_queryset(self.text).get(
            role=Role.EDITOR
        )

        restarted = restart_workflow_from_stage(
            self.text,
            StageType.FIRST_PROOFREADING,
            self.superuser,
        )

        self.text.refresh_from_db()

        self.assertEqual(self.text.current_workflow_cycle, 2)
        self.assertEqual(restarted.workflow_cycle, 2)
        self.assertEqual(restarted.iteration, 1)
        self.assertIsNone(restarted.started_at)

        self.assertTrue(
            WorkflowStage.objects.filter(
                pk=editing.pk,
                workflow_cycle=1,
            ).exists()
        )
        self.assertTrue(
            WorkflowRoleAssignment.objects.filter(
                pk=old_assignment.pk,
                workflow_cycle=1,
            ).exists()
        )
        self.assertTrue(
            current_assignment_queryset(self.text).filter(
                role=Role.EDITOR,
                assigned_to=self.editor,
            ).exists()
        )

    def test_restart_to_ready_for_editing_does_not_copy_assignments(self):
        self.begin_editing()

        restart_workflow_from_stage(
            self.text,
            StageType.READY_FOR_EDITING,
            self.superuser,
        )

        self.assertFalse(
            current_assignment_queryset(self.text).exists()
        )

    def test_restart_does_not_assign_work_to_former_member(self):
        self.begin_editing()
        Person.objects.filter(user=self.editor).update(is_active=False)

        restart_workflow_from_stage(
            self.text,
            StageType.FIRST_PROOFREADING,
            self.superuser,
        )

        assignment = current_assignment_queryset(self.text).get(
            role=Role.EDITOR
        )

        self.assertIsNone(assignment.assigned_to_id)
        self.assertIsNone(assignment.assigned_at)

    def test_old_stage_cannot_be_modified_after_restart(self):
        editing = self.begin_editing()

        restart_workflow_from_stage(
            self.text,
            StageType.READY_FOR_EDITING,
            self.superuser,
        )

        with self.assertRaises(ValidationError):
            finish_stage_record(editing, self.today)

        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

    def test_stale_text_instance_cannot_mutate_new_cycle(self):
        stale_text = Text.objects.get(pk=self.text.pk)

        restart_workflow_from_stage(
            self.text,
            StageType.READY_FOR_EDITING,
            self.superuser,
        )

        with self.assertRaises(ValidationError):
            claim_ready_for_editing(stale_text, self.editor)

        self.assertFalse(
            current_assignment_queryset(self.text).exists()
        )

    def test_restart_can_reopen_withdrawn_text(self):
        WorkflowStage.objects.create(
            text=self.text,
            stage_type=StageType.WITHDRAWN,
            workflow_cycle=1,
        )

        with self.assertRaises(ValidationError):
            claim_ready_for_editing(self.text, self.editor)

        restart_workflow_from_stage(
            self.text,
            StageType.READY_FOR_EDITING,
            self.superuser,
        )
        editing = claim_ready_for_editing(self.text, self.editor)

        self.assertEqual(editing.workflow_cycle, 2)

    def test_restart_rejects_terminal_stage(self):
        for stage_type in (StageType.READY, StageType.WITHDRAWN):
            with self.subTest(stage=stage_type):
                with self.assertRaises(ValidationError):
                    restart_workflow_from_stage(
                        self.text,
                        stage_type,
                        self.superuser,
                    )

        self.text.refresh_from_db()
        self.assertEqual(self.text.current_workflow_cycle, 1)


class WorkflowIntegrityTests(WorkflowTestDataMixin, TestCase):
    def test_stage_uniqueness_is_enforced_by_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkflowStage.objects.create(
                    text=self.text,
                    workflow_cycle=1,
                    stage_type=StageType.READY_FOR_EDITING,
                    iteration=1,
                )

    def test_invalid_stage_dates_are_rejected_by_database(self):
        cases = (
            {
                "started_at": None,
                "ended_at": self.today,
            },
            {
                "started_at": self.today,
                "ended_at": self.today - timedelta(days=1),
            },
            {
                "started_at": self.today,
                "ended_at": None,
                "is_completed": True,
            },
        )

        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        WorkflowStage.objects.create(
                            text=self.text,
                            stage_type=StageType.EDITING,
                            **values,
                        )

    def test_primary_verifiers_must_differ_at_database_level(self):
        WorkflowRoleAssignment.objects.create(
            text=self.text,
            role=Role.VERIFIER_1,
            assigned_to=self.verifier_1,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkflowRoleAssignment.objects.create(
                    text=self.text,
                    role=Role.VERIFIER_2,
                    assigned_to=self.verifier_1,
                )

    def test_failed_handover_does_not_complete_editing(self):
        editing = self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )

        with self.assertRaises(ValidationError):
            send_to_first_verification(
                self.text,
                self.editor,
                ended_at=self.today - timedelta(days=1),
            )

        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)
        self.assertIsNone(editing.ended_at)

    def test_failed_start_rolls_back_new_assignment(self):
        create_pending_stage(self.text, StageType.FIRST_PROOFREADING)

        WorkflowStage.objects.create(
            text=self.text,
            stage_type=StageType.EDITING_CONTROL,
            started_at=self.today + timedelta(days=1),
            ended_at=self.today + timedelta(days=2),
            is_completed=True,
        )

        with self.assertRaises(ValidationError):
            claim_stage(
                self.text,
                StageType.FIRST_PROOFREADING,
                self.proofreader,
                started_at=self.today,
            )

        self.assertFalse(
            current_assignment_queryset(self.text).filter(
                role=Role.PROOFREADER_1,
            ).exists()
        )
        self.assertIsNone(
            self.stage(StageType.FIRST_PROOFREADING).started_at
        )

    def test_notes_only_save_does_not_change_assignee_or_timestamp(self):
        assignment = WorkflowRoleAssignment.objects.create(
            text=self.text,
            role=Role.EDITOR,
            assigned_to=self.editor,
        )
        original_assigned_at = assignment.assigned_at

        assignment.assigned_to = self.other_editor
        assignment.notes = "Zmiana samej notatki."
        assignment.save(update_fields=["notes"])
        assignment.refresh_from_db()

        self.assertEqual(assignment.assigned_to_id, self.editor.pk)
        self.assertEqual(assignment.assigned_at, original_assigned_at)
        self.assertEqual(assignment.notes, "Zmiana samej notatki.")

    def test_explicit_unassignment_clears_timestamp(self):
        assignment = WorkflowRoleAssignment.objects.create(
            text=self.text,
            role=Role.EDITOR,
            assigned_to=self.editor,
        )

        assignment.assigned_to = None
        assignment.save(update_fields=["assigned_to"])
        assignment.refresh_from_db()

        self.assertIsNone(assignment.assigned_to_id)
        self.assertIsNone(assignment.assigned_at)

    def test_user_deletion_preserves_historical_assignment_timestamp(self):
        assignment = WorkflowRoleAssignment.objects.create(
            text=self.text,
            role=Role.EDITOR,
            assigned_to=self.editor,
        )
        original_assigned_at = assignment.assigned_at

        self.editor.delete()
        assignment.refresh_from_db()

        self.assertIsNone(assignment.assigned_to_id)
        self.assertEqual(assignment.assigned_at, original_assigned_at)


class WorkflowConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_only_one_editor_can_claim_same_text(self):
        first_user = create_member("pierwszy_rownolegly", "Redaktor")
        second_user = create_member("drugi_rownolegly", "Redaktor")

        text = Text.objects.create(
            title="Równoległy przydział",
            length=25000,
        )
        WorkflowStage.objects.create(
            text=text,
            stage_type=StageType.READY_FOR_EDITING,
        )

        barrier = Barrier(2)
        text_id = text.pk

        def attempt_claim(user_id):
            close_old_connections()

            try:
                connection = connections["default"]

                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SET lock_timeout = '10s'")

                local_text = Text.objects.get(pk=text_id)
                local_user = User.objects.get(pk=user_id)

                barrier.wait(timeout=10)

                try:
                    claim_ready_for_editing(local_text, local_user)
                except ValidationError:
                    return "rejected"

                return "claimed"
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(attempt_claim, first_user.pk),
                executor.submit(attempt_claim, second_user.pk),
            ]
            results = [future.result(timeout=30) for future in futures]

        self.assertCountEqual(results, ["claimed", "rejected"])
        self.assertEqual(
            WorkflowRoleAssignment.objects.filter(
                text=text,
                workflow_cycle=1,
                role=Role.EDITOR,
                assigned_to__isnull=False,
            ).count(),
            1,
        )
        self.assertEqual(
            WorkflowStage.objects.filter(
                text=text,
                workflow_cycle=1,
                stage_type=StageType.EDITING,
                is_completed=False,
            ).count(),
            1,
        )