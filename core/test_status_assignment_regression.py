from importlib import import_module
from io import StringIO
from tempfile import TemporaryDirectory

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import _text_team_members
from people.models import Person
from texts.models import Anthology, Text
from workflow.admin_performers import correct_stage_performers
from workflow.admin_status import set_admin_status
from workflow.labels import assignment_label
from workflow.models import WorkflowHandoff as H, WorkflowRepetition as R
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.services import STAGE_ROLES


repair = import_module('workflow.migrations.0011_merge_status_assignments').repair_status_assignments


class StatusAssignmentFixtures(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        User = get_user_model()
        self.admin = User.objects.create_superuser('admin', 'admin@example.com', 'test')
        self.member = User.objects.create_user('member', 'member@example.com', 'test')
        Person.objects.create(user=self.member, first_name='Jan', last_name='Test', email=self.member.email)
        self.other = User.objects.create_user('other', 'other@example.com', 'test')
        Person.objects.create(user=self.other, first_name='Anna', last_name='Test', email=self.other.email)
        self.book = Anthology.objects.create(title='Test')
        self.text = Text.objects.create(title='Tekst', length=100, anthology=self.book)
        self.today = timezone.localdate()

    def status(self, kind, text=None):
        text = text or self.text
        return set_admin_status(text.pk, kind, self.admin, version_of(text))


class StatusAssignmentRegressionTests(StatusAssignmentFixtures):
    def test_status_preserves_every_existing_operational_role(self):
        for kind, role in STAGE_ROLES.items():
            with self.subTest(kind=kind):
                text = Text.objects.create(title=kind, length=100, anthology=self.book)
                assignment = A.objects.create(text=text, role=role, assigned_to=self.admin)
                original_date = assignment.assigned_at
                stage = self.status(kind, text)
                assignment.refresh_from_db()
                self.assertEqual(stage.assignment_id, assignment.pk)
                self.assertEqual(stage.execution_number, assignment.execution_number)
                self.assertTrue(assignment.is_current)
                self.assertEqual(assignment.assigned_to_id, self.admin.pk)
                self.assertEqual(assignment.assigned_at, original_date)
                self.assertEqual(A.objects.filter(text=text).count(), 1)

    def test_status_preserves_downstream_team_and_completed_history(self):
        assignments = []
        for kind, role in (('first_verification', 'verifier_1'),
                           ('editing_control', 'editing_coordinator'),
                           ('first_proofreading', 'proofreader_1')):
            assignment = A.objects.create(text=self.text, role=role, assigned_to=self.member)
            stage = S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                                     started_at=self.today, ended_at=self.today, is_completed=True)
            assignments.append((assignment, stage))
        reopened = self.status('first_verification')
        self.assertEqual(reopened.assignment_id, assignments[0][0].pk)
        for assignment, stage in assignments:
            assignment.refresh_from_db()
            stage.refresh_from_db()
            self.assertTrue(assignment.is_current)
            self.assertEqual(assignment.assigned_to_id, self.member.pk)
            self.assertTrue(stage.is_completed)
            self.assertEqual(stage.ended_at, self.today)
        self.assertEqual(A.objects.filter(text=self.text).count(), 3)

    def test_admin_status_post_keeps_verifier_identity_and_role(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('admin:texts_text_manual_status', args=[self.text.pk]),
                                    {'stage': 'first_verification', 'version': version_of(self.text),
                                     'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        stage = S.objects.current_cycle().get(text=self.text, stage_type='first_verification')
        self.assertEqual(stage.assignment_id, assignment.pk)
        self.assertEqual(assignment_label(assignment), 'Weryfikator 1')

    def test_new_role_is_created_only_once_and_reused_on_status_correction(self):
        stage = self.status('editing_control')
        second = self.status('editing_control')
        self.assertEqual(second.assignment_id, stage.assignment_id)
        self.assertEqual(second.execution_number, 1)
        self.assertEqual(A.objects.filter(text=self.text).count(), 1)

    def test_reserved_work_still_blocks_status_correction(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        stage = S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment)
        with self.assertRaises(ValidationError):
            self.status('editing_control')
        stage.refresh_from_db()
        self.assertTrue(stage.is_current)
        self.assertEqual(A.objects.filter(text=self.text).count(), 1)

    def test_stage_performer_correction_keeps_canonical_assignment(self):
        for kind, role in (('first_verification', 'verifier_1'),
                           ('editing_control', 'editing_coordinator')):
            with self.subTest(role=role):
                assignment = A.objects.create(text=self.text, role=role, assigned_to=self.member)
                stage = S.objects.create(text=self.text, stage_type=kind, assignment=assignment)
                correct_stage_performers(self.text.pk, {stage.pk: self.other},
                                         self.admin, version_of(self.text))
                assignment.refresh_from_db()
                stage.refresh_from_db()
                self.assertEqual(stage.assignment_id, assignment.pk)
                self.assertEqual(assignment.role, role)
                self.assertEqual(assignment.assigned_to_id, self.other.pk)
                self.assertEqual(A.objects.filter(text=self.text, role=role).count(), 1)

    def test_team_labels_keep_role_names_in_current_and_previous_executions(self):
        current = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member,
                                    execution_number=2)
        old = A.objects.create(text=self.text, role='editing_coordinator', assigned_to=self.other,
                                is_current=False)
        members = _text_team_members(self.text, {'verifier_1': current})
        self.assertEqual(next(row['label'] for row in members if row['role'] == current.role),
                         'Weryfikator 1 (wyk. 2)')
        self.assertEqual(next(row['label'] for row in members if row.get('is_previous')),
                         old.get_role_display())
        self.assertFalse(any(row['label'].startswith(('Pierwsza weryfikacja', 'Kontrola K. redakcji'))
                             for row in members))


class StatusAssignmentMergeTests(StatusAssignmentFixtures):
    def broken_status(self, *, role='verifier_1', kind='first_verification'):
        canonical = A.objects.create(text=self.text, role=role, assigned_to=self.member)
        completed = S.objects.create(text=self.text, stage_type=kind, assignment=canonical,
                                      started_at=self.today, ended_at=self.today, is_completed=True,
                                      is_current=False, is_released=False)
        A.objects.filter(pk=canonical.pk).update(is_current=False)
        empty = A.objects.create(text=self.text, role=role, execution_number=2)
        pending = S.objects.create(text=self.text, stage_type=kind, assignment=empty,
                                   iteration=2, execution_number=2)
        return canonical, completed, empty, pending

    def test_merge_restores_original_identity_dates_and_stage_links(self):
        for role, kind in (('verifier_1', 'first_verification'),
                           ('editing_coordinator', 'editing_control')):
            with self.subTest(role=role):
                canonical, completed, empty, pending = self.broken_status(role=role, kind=kind)
                known_date = canonical.assigned_at
                report = repair(apps, 'default')
                self.assertEqual(report['merged'], 1)
                canonical.refresh_from_db()
                completed.refresh_from_db()
                pending.refresh_from_db()
                self.assertFalse(A.objects.filter(pk=empty.pk).exists())
                self.assertTrue(canonical.is_current)
                self.assertEqual(canonical.assigned_to_id, self.member.pk)
                self.assertEqual(canonical.assigned_at, known_date)
                self.assertEqual(pending.assignment_id, canonical.pk)
                self.assertEqual(pending.execution_number, canonical.execution_number)
                self.assertTrue(completed.is_completed)
                self.assertEqual(completed.ended_at, self.today)
                self.assertGreater(version_of(self.text), 0)
                self.assertEqual(repair(apps, 'default')['merged'], 0)

    def test_command_preview_does_not_write_and_apply_is_idempotent(self):
        canonical, completed, empty, pending = self.broken_status()
        output = StringIO()
        before_version = version_of(self.text)
        call_command('repair_status_assignments', stdout=output)
        self.assertIn('Puste duplikaty: 1', output.getvalue())
        self.assertEqual(version_of(self.text), before_version)
        self.assertTrue(A.objects.filter(pk=empty.pk, is_current=True).exists())
        call_command('repair_status_assignments', apply=True, stdout=StringIO())
        self.assertFalse(A.objects.filter(pk=empty.pk).exists())
        self.assertEqual(repair(apps, 'default')['merged'], 0)

    def test_repair_runs_with_historical_migration_models(self):
        canonical, completed, empty, pending = self.broken_status()
        historical = MigrationExecutor(connection).loader.project_state([
            ('workflow', '0010_workflowstage_queued_at'),
            ('core', '0010_editrevision'),
        ]).apps
        self.assertEqual(repair(historical, 'default')['merged'], 1)
        pending.refresh_from_db()
        self.assertEqual(pending.assignment_id, canonical.pk)
        self.assertFalse(A.objects.filter(pk=empty.pk).exists())

    def test_repair_merges_a_chain_of_empty_status_executions(self):
        canonical, completed, empty, pending = self.broken_status()
        A.objects.filter(pk=empty.pk).update(is_current=False)
        S.objects.filter(pk=pending.pk).update(is_current=False, is_released=False)
        latest = A.objects.create(text=self.text, role='verifier_1', execution_number=3)
        current = S.objects.create(text=self.text, stage_type='first_verification',
                                    iteration=3, execution_number=3, assignment=latest)
        self.assertEqual(repair(apps, 'default')['merged'], 2)
        self.assertEqual(A.objects.filter(text=self.text).count(), 1)
        for stage in (pending, current):
            stage.refresh_from_db()
            self.assertEqual(stage.assignment_id, canonical.pk)
            self.assertEqual(stage.execution_number, 1)

    def test_merge_keeps_assigned_replacement(self):
        canonical, completed, empty, pending = self.broken_status()
        A.objects.filter(pk=empty.pk).update(assigned_to=self.other)
        self.assertEqual(repair(apps, 'default')['merged'], 0)
        self.assertTrue(A.objects.filter(pk=empty.pk, assigned_to=self.other).exists())

    def test_merge_keeps_started_work(self):
        canonical, completed, empty, pending = self.broken_status()
        S.objects.filter(pk=pending.pk).update(started_at=self.today)
        report = repair(apps, 'default')
        self.assertEqual(report['merged'], 0)
        self.assertEqual(len(report['skipped']), 1)
        self.assertTrue(A.objects.filter(pk=empty.pk, is_current=True).exists())

    def test_merge_keeps_repetitions_and_restore_snapshots(self):
        canonical, completed, empty, pending = self.broken_status()
        run = R.objects.create(text=self.text, previous_assignment_ids=[canonical.pk],
                               previous_stage_ids=[completed.pk])
        self.assertEqual(repair(apps, 'default')['merged'], 0)
        R.objects.filter(pk=run.pk).update(previous_assignment_ids=[], previous_stage_ids=[])
        A.objects.filter(pk=empty.pk).update(repetition=run)
        self.assertEqual(repair(apps, 'default')['merged'], 0)

    def test_merge_keeps_handoff_participants(self):
        canonical, completed, empty, pending = self.broken_status()
        H.objects.create(text=self.text, stage=pending, previous_assignment=canonical,
                          new_assignment=empty, actor=self.admin, reason='Test')
        self.assertEqual(repair(apps, 'default')['merged'], 0)
        self.assertTrue(A.objects.filter(pk=empty.pk).exists())

    def test_merge_never_assigns_same_person_to_both_verifications(self):
        canonical, completed, empty, pending = self.broken_status()
        A.objects.create(text=self.text, role='verifier_2', assigned_to=self.member)
        report = repair(apps, 'default')
        self.assertEqual(report['merged'], 0)
        self.assertEqual(len(report['skipped']), 1)
        canonical.refresh_from_db()
        self.assertFalse(canonical.is_current)

    def test_merge_never_touches_old_cycles_or_different_stage_roles(self):
        canonical, completed, empty, pending = self.broken_status()
        Text.objects.filter(pk=self.text.pk).update(current_workflow_cycle=2)
        self.assertEqual(repair(apps, 'default')['merged'], 0)
        Text.objects.filter(pk=self.text.pk).update(current_workflow_cycle=1)
        S.objects.filter(pk=pending.pk).update(stage_type='editing_control')
        self.assertEqual(repair(apps, 'default')['merged'], 0)
