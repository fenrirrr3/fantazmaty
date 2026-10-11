"""Spójność danych procesu w bazie: ograniczenia etapów i przypisań, daty przypisania oraz równoczesne przejęcie tekstu."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature, tag

from fantazmaty.testing import create_member
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from texts.models import Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import (
    claim_ready_for_editing,
    claim_stage,
    create_pending_stage,
    current_assignment_queryset,
    send_to_first_verification,
)

User = get_user_model()
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role
Assignment = WorkflowRoleAssignment


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


class VerifierConstraintTests(TestCase):
    """Ograniczenie „różni weryfikatorzy 1 i 2” działa też przy zapisie zbiorczym i aktualizacji (także na MySQL)."""

    def setUp(self):
        self.text = Text.objects.create(title="Test ograniczenia", length=100)
        self.user = get_user_model().objects.create_user(username="verifier_constraint")

    def create(self, role, **kwargs):
        return Assignment.objects.create(
            text=self.text, role=role, assigned_to=self.user, **kwargs
        )

    def test_bulk_insert_cannot_assign_both_verifications(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.bulk_create([
                Assignment(text=self.text, role=role, assigned_to=self.user)
                for role in (Assignment.Role.VERIFIER_1, Assignment.Role.VERIFIER_2)
            ])

    def test_direct_update_cannot_bypass_constraint(self):
        self.create(Assignment.Role.VERIFIER_1)
        other = self.create(Assignment.Role.EDITOR)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.filter(pk=other.pk).update(role=Assignment.Role.VERIFIER_2)

    def test_other_roles_cycles_and_texts_are_allowed(self):
        self.create(Assignment.Role.VERIFIER_1)
        self.create(Assignment.Role.EDITOR)
        self.create(Assignment.Role.PROOFREADER_1)
        self.create(Assignment.Role.VERIFIER_2, workflow_cycle=2)
        second = Text.objects.create(title="Inny tekst", length=100)
        Assignment.objects.create(text=second, role=Assignment.Role.VERIFIER_2, assigned_to=self.user)

    def test_unassigned_roles_and_deleted_user_are_allowed(self):
        self.create(Assignment.Role.VERIFIER_1)
        Assignment.objects.create(text=self.text, role=Assignment.Role.VERIFIER_2)
        self.user.delete()
        self.assertEqual(Assignment.objects.filter(text=self.text, assigned_to__isnull=True).count(), 2)


@tag('slow')
class WorkflowConcurrencyTests(TransactionTestCase):
    """Dwa wątki i prawdziwe transakcje; czyszczenie wszystkich tabel po teście jest wolne (zwłaszcza na MySQL)."""

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
