from importlib import import_module
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.edit_versions import version_of
from core.selectors.texts import text_detail_context
from core.test_status_assignment_regression import StatusAssignmentFixtures
from workflow.admin_performers import correct_stage_performers
from workflow.admin_stage_dates import editing_transition_choices
from workflow.import_context import importing_completed
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.services import send_to_first_verification, completed_stage_exists


class AssignmentMergeOctoberTests(StatusAssignmentFixtures):
    def duplicate(self):
        empty = A.objects.create(text=self.text, role='verifier_1')
        old = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member,
                               execution_number=2, is_current=False)
        token = importing_completed.set(True)
        try:
            stage = S.objects.create(text=self.text, stage_type='first_verification',
                                     assignment=old, is_completed=True, imported_completed=True, is_current=False,
                                     is_released=False)
        finally:
            importing_completed.reset(token)
        return empty, old, stage

    def test_admin_replaces_person_and_merges_empty_and_historical_role(self):
        empty, old, stage = self.duplicate()
        correct_stage_performers(self.text.pk, {stage.pk: self.other}, self.admin,
                                 version_of(self.text))
        empty.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        self.assertEqual(empty.assigned_to_id, self.other.pk)
        self.assertTrue(empty.is_current)
        self.assertEqual(stage.assignment_id, empty.pk)
        self.assertTrue(stage.is_completed)
        self.assertFalse(stage.is_current)
        self.assertIsNone(stage.ended_at)

    def test_retired_migration_preserves_historical_assignments(self):
        empty, old, stage = self.duplicate()
        state = MigrationExecutor(connection).loader.project_state(
            [('workflow', '0012_restore_retired_team_assignments')])
        migration = import_module(
            'workflow.migrations.0013_merge_duplicate_role_assignments'
        ).Migration('0013_merge_duplicate_role_assignments', 'workflow')
        assignments = list(A.objects.filter(text=self.text).order_by('pk').values())
        stages = list(S.objects.filter(text=self.text).order_by('pk').values())
        migration.apply(state.clone(), None)
        migration.unapply(state.clone(), None)
        self.assertEqual(list(A.objects.filter(text=self.text).order_by('pk').values()), assignments)
        self.assertEqual(list(S.objects.filter(text=self.text).order_by('pk').values()), stages)

    def test_archived_dateless_verification_blocks_first_and_offers_second(self):
        self.duplicate()
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.admin)
        editing = S.objects.create(text=self.text, stage_type='editing', assignment=editor,
                                   started_at=self.today)
        context = text_detail_context(user=self.admin, text=self.text)
        self.assertFalse(context['can_send_to_first_verification'])
        self.assertTrue(context['can_send_to_second_verification'])
        self.assertIn('second_verification', dict(editing_transition_choices(self.text)))
        with self.assertRaises(ValidationError):
            send_to_first_verification(self.text, self.admin)
        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

    def test_reservation_alone_does_not_mark_verification_completed(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment)
        self.assertFalse(completed_stage_exists(self.text, 'first_verification'))

    def test_editor_exchange_history_survives_assignment_merge(self):
        first = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        second = A.objects.create(text=self.text, role='editor', assigned_to=self.other,
                                  execution_number=2, is_current=False)
        rows = []
        for index, kind in enumerate(('editing', 'author_editing', 'editing'), 1):
            rows.append(S.objects.create(text=self.text, stage_type=kind, assignment=second,
                        iteration=index, execution_number=2, started_at=self.today,
                        ended_at=self.today, is_completed=True, is_current=False))
        before = list(S.objects.filter(text=self.text).values_list(
            'pk', 'stage_type', 'iteration', 'execution_number', 'started_at', 'ended_at'))
        correct_stage_performers(self.text.pk, {rows[0].pk: self.admin}, self.admin,
                                 version_of(self.text))
        self.assertEqual(before, list(S.objects.filter(text=self.text).values_list(
            'pk', 'stage_type', 'iteration', 'execution_number', 'started_at', 'ended_at')))
        self.assertEqual(set(S.objects.filter(text=self.text).values_list('assignment_id', flat=True)),
                         {first.pk})
