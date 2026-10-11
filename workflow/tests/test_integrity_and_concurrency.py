"""Spójność danych procesu: walidacja i ograniczenia etapów i przypisań (także pracy tylko z importu), daty przypisania,
opisy rekordów oraz równoczesne przejęcie tekstu."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature, tag
from django.utils import timezone

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

from .support import importing

User = get_user_model()
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role
Assignment = WorkflowRoleAssignment


class WorkflowIntegrityTests(WorkflowTestDataMixin, TestCase):
    """Ograniczenia bazy i wycofanie nieudanych przejść."""

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

    @classmethod
    def setUpTestData(cls):
        cls.text = Text.objects.create(title="Test ograniczenia", length=100)
        cls.user = get_user_model().objects.create_user(username="verifier_constraint")

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



class RecordValidationTests(TestCase):
    """Walidacja modeli: niespójne daty i stany, praca tylko z importu i reguły wykonawców."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', 'admin@example.test', 'test')
        cls.member = create_member('member', 'Weryfikator')
        cls.text = Text.objects.create(title='Tekst', length=100)
        cls.today = timezone.localdate()

    def errors(self, record):
        with self.assertRaises(ValidationError) as caught:
            record.full_clean()
        return caught.exception.message_dict

    def test_stage_dates_and_states_must_agree(self):
        future = self.today + timedelta(days=1)
        WorkflowStage.objects.create(text=self.text, stage_type='editing', started_at=self.today)
        for fields, field, message in (
                ({'is_completed': True, 'started_at': future, 'ended_at': future}, 'started_at', 'przyszłej daty'),
                ({'is_completed': True, 'started_at': self.today, 'ended_at': future}, 'ended_at', 'przyszłą datą'),
                ({'is_completed': True, 'ended_at': self.today}, 'started_at', 'musi mieć datę rozpoczęcia'),
                ({'started_at': self.today, 'ended_at': self.today}, 'is_completed', 'oznaczony jako zakończony'),
                ({'is_completed': True}, 'ended_at', 'musi mieć datę zakończenia'),
                ({'iteration': 2}, 'stage_type', 'istnieje już otwarty etap')):
            with self.subTest(fields=fields):
                stage = WorkflowStage(text=self.text, stage_type='editing', **fields)
                self.assertIn(message, ' '.join(self.errors(stage)[field]))
        with importing():
            stage = WorkflowStage(text=self.text, stage_type='first_proofreading', imported_completed=True)
            self.assertIn('pozostać zakończony', ' '.join(self.errors(stage)['is_completed']))

    def test_import_only_work_is_closed_history_written_by_the_importer(self):
        verifier_4 = Assignment(text=self.text, role='verifier_4', assigned_to=self.member, is_current=False)
        wrong_role = Assignment.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        with importing():
            self.assertIn('nie może być bieżącym', str(self.errors(Assignment(text=self.text, role='verifier_4'))))
            verifier_4.save()
            closed = dict(text=self.text, stage_type='fourth_verification', is_completed=True,
                          imported_completed=True, is_current=False, is_released=False)
            for stage, message in ((WorkflowStage(**dict(closed, is_current=True), assignment=verifier_4),
                                    'wyłącznie jako zakończona praca z importu'),
                                   (WorkflowStage(**closed), 'wymaga powiązanego przypisania'),
                                   (WorkflowStage(**closed, assignment=wrong_role), 'nie odpowiada importowanemu')):
                with self.subTest(message=message):
                    self.assertIn(message, str(self.errors(stage)))
            stage = WorkflowStage.objects.create(**closed, assignment=verifier_4)
            another = Assignment.objects.create(text=self.text, role='verifier_4', assigned_to=self.admin,
                                                execution_number=2, is_current=False)
        stage.assignment = another
        self.assertIn('wyłącznie importer', str(self.errors(stage)))
        verifier_4.assigned_to = self.admin
        with self.assertRaisesMessage(ValidationError, 'wyłącznie importer'):
            verifier_4.save()
        ordinary = WorkflowStage(text=self.text, stage_type='first_proofreading', imported_completed=True,
                                 is_completed=True)
        self.assertIn('wyłącznie importer zakończonych etapów', str(self.errors(ordinary)))

    def test_assignment_rules_for_styling_and_verifiers(self):
        styling = Assignment(text=self.text, role='styling', assigned_to=self.member)
        self.assertIn('assigned_to', self.errors(styling))
        Assignment(text=self.text, role='styling', assigned_to=self.admin).full_clean()
        Assignment.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        second = Assignment(text=self.text, role='verifier_2', assigned_to=self.member)
        self.assertIn('różne osoby', ' '.join(self.errors(second)['assigned_to']))

    def test_saving_records_fills_queue_date_and_ignores_empty_update(self):
        stage = WorkflowStage.objects.create(text=self.text, stage_type='first_proofreading')
        WorkflowStage.objects.filter(pk=stage.pk).update(queued_at=None)
        stage.refresh_from_db()
        stage.save(update_fields=['is_released'])
        stage.refresh_from_db()
        self.assertEqual(stage.queued_at, self.today)
        assignment = Assignment.objects.create(text=self.text, role='editor', assigned_to=self.member)
        assignment.assigned_to = None
        assignment.save(update_fields=[])
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_to, self.member)

    def test_records_describe_text_role_person_and_execution(self):
        from workflow.models import WorkflowRepetition

        stage = WorkflowStage.objects.create(text=self.text, stage_type='editing', execution_number=2, iteration=2)
        self.assertEqual(str(stage), 'Tekst: Redakcja – przebieg 1 (wyk. 2)')
        empty = Assignment.objects.create(text=self.text, role='editor')
        self.assertEqual(str(empty), 'Tekst – Redaktor – nieprzypisane – przebieg 1')
        run = WorkflowRepetition.objects.create(text=self.text)
        self.assertEqual(str(run), f'Tekst – powtórzenie #{run.pk}')

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
