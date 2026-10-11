"""Naprawy przypisań po zmianie statusu: migracje 0011–0013 i polecenie repair_status_assignments."""
import re
from datetime import date, timedelta
from importlib import import_module
from io import StringIO

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import ProjectState
from django.urls import reverse

from core.edit_versions import version_of
from core.selectors.texts import text_detail_context
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from texts.models import Text
from workflow.models import WorkflowHandoff as H, WorkflowRepetition as R
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages
from workflow.services import resume_editing, send_to_second_verification

from .support import add_roles, importing

repair = import_module('workflow.migrations.0011_merge_status_assignments').repair_status_assignments
restore = import_module('workflow.migrations.0012_restore_retired_team_assignments').restore_team_assignments


class StatusAssignmentMergeTests(StatusAssignmentFixtures):
    """Migracja 0011: pusty duplikat roli po zmianie statusu wraca do pierwotnego przypisania."""

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


class RetiredTeamAssignmentTests(StatusAssignmentFixtures):
    """Migracja 0012: przywrócenie zespołu, gdy status zarchiwizował przypisanie weryfikatora (przypadek ze zrzutu ekranu)."""

    def screenshot_state(self):
        person = self.member.person_profile
        person.first_name = 'Renata'
        person.save()
        self.member.refresh_from_db()
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.admin)
        editing = S.objects.create(text=self.text, stage_type='editing', assignment=editor,
                                    started_at=date(2026, 9, 30))
        verifier = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member,
                                     is_current=False)
        verified = S.objects.create(text=self.text, stage_type='first_verification', assignment=verifier,
                                     started_at=date(2026, 10, 1), ended_at=date(2026, 10, 1),
                                     is_completed=True, is_current=False, is_released=False)
        author = S.objects.create(text=self.text, stage_type='author_editing', assignment=editor,
                                   started_at=date(2026, 7, 5), ended_at=date(2026, 7, 23),
                                   is_completed=True, is_current=False, is_released=False)
        return editor, editing, verifier, verified, author

    def team_sections(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        sections = re.findall(r'<details class="text-team"[^>]*>(.*?)</details>', response.content.decode(), re.S)
        current = next(section for section in sections if 'Osoby przypisane do tekstu' in section)
        previous = next(section for section in sections if 'Historia przypisań osób' in section)
        return current, previous

    def test_screenshot_case_restores_team_even_when_0011_finds_zero_duplicates(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        known_date = verifier.assigned_at
        self.assertEqual(repair(apps, 'default')['restored'], 0)
        before_version = version_of(self.text)
        report = restore(apps, 'default')
        self.assertEqual((report['restored'], report['merged']), (1, 0))
        verifier.refresh_from_db()
        verified.refresh_from_db()
        editing.refresh_from_db()
        author.refresh_from_db()
        self.assertTrue(verifier.is_current)
        self.assertEqual(verifier.assigned_to_id, self.member.pk)
        self.assertEqual(verifier.assigned_at, known_date)
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        self.assertEqual(verified.assignment_id, verifier.pk)
        self.assertTrue(verified.is_completed)
        self.assertFalse(verified.is_current)
        self.assertEqual(verified.ended_at, date(2026, 10, 1))
        self.assertEqual(editing.assignment_id, editor.pk)
        self.assertTrue(editing.is_current)
        self.assertEqual(editing.started_at, date(2026, 9, 30))
        self.assertEqual(author.ended_at, date(2026, 7, 23))
        self.assertGreater(version_of(self.text), before_version)
        context = text_detail_context(user=self.admin, text=self.text)
        members = [row for row in context['team_members'] if row['role'] == 'verifier_1']
        self.assertEqual(len(members), 1)
        self.assertTrue(members[0]['is_assigned'])
        self.assertEqual(members[0]['user']['pk'], self.member.pk)
        self.assertFalse(any(row['role'] == 'verifier_1' for row in context['previous_team_members']))
        self.assertEqual(restore(apps, 'default')['restored'], 0)

    def test_template_moves_verifier_from_history_to_current_team_after_repair(self):
        self.screenshot_state()
        current, previous = self.team_sections()
        self.assertEqual(current.count('Weryfikator 1'), 1)
        self.assertNotIn('Renata', current)
        self.assertIn('Renata', previous)
        self.assertIn('przebieg 1', previous)
        restore(apps, 'default')
        current, previous = self.team_sections()
        self.assertEqual(current.count('Weryfikator 1'), 1)
        self.assertIn('Renata', current)
        # The history section is always present; after the repair it has no entries.
        self.assertIn('Brak wcześniejszych przypisań', previous)

    def test_next_handover_uses_second_verifier_and_preserves_the_first(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        restore(apps, 'default')
        second = A.objects.create(text=self.text, role='verifier_2', assigned_to=self.other)
        with self.assertRaises(ValidationError):
            send_to_second_verification(self.text, self.admin, self.today)
        # Dated live W1 requires a new editorial pass; restoring an assignment alone is not a handoff.
        S.objects.filter(pk=editing.pk).update(is_completed=True, ended_at=verified.ended_at)
        resume_editing(self.text, self.admin, self.today)
        next_stage = send_to_second_verification(self.text, self.admin, self.today)
        self.assertEqual(next_stage.assignment_id, second.pk)
        self.assertEqual(next_stage.assignment.assigned_to_id, self.other.pk)
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        verified.refresh_from_db()
        self.assertTrue(verified.is_completed)
        self.assertFalse(verified.is_current)

    def test_command_inspects_previews_and_applies_restoration(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        before = version_of(self.text)
        output = StringIO()
        call_command('repair_status_assignments', inspect_text=self.text.title, stdout=output)
        self.assertIn(f'Przydział {verifier.pk}: verifier_1', output.getvalue())
        self.assertIn(f'Etap {verified.pk}: first_verification', output.getvalue())
        self.assertIn('aktualny=False', output.getvalue())
        output = StringIO()
        call_command('repair_status_assignments', stdout=output)
        self.assertIn('Puste duplikaty: 0; przypisania do przywrócenia: 1.', output.getvalue())
        self.assertEqual(version_of(self.text), before)
        verifier.refresh_from_db()
        self.assertFalse(verifier.is_current)
        call_command('repair_status_assignments', apply=True, stdout=StringIO())
        verifier.refresh_from_db()
        self.assertTrue(verifier.is_current)

    def test_restore_uses_historical_models_after_applied_0011(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        historical = MigrationExecutor(connection).loader.project_state([
            ('workflow', '0011_merge_status_assignments'),
        ]).apps
        self.assertEqual(restore(historical, 'default')['restored'], 1)
        verifier.refresh_from_db()
        self.assertTrue(verifier.is_current)

    def test_retired_empty_tail_is_merged_without_a_current_placeholder_stage(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        empty = A.objects.create(text=self.text, role='verifier_1', execution_number=2, is_current=False)
        obsolete = S.objects.create(text=self.text, stage_type='first_verification', assignment=empty,
                                     iteration=2, execution_number=2, is_current=False, is_released=False)
        self.assertEqual(repair(apps, 'default')['merged'], 0)
        report = restore(apps, 'default')
        self.assertEqual((report['restored'], report['merged']), (1, 1))
        obsolete.refresh_from_db()
        self.assertEqual(obsolete.assignment_id, verifier.pk)
        self.assertFalse(obsolete.is_current)
        self.assertFalse(A.objects.filter(pk=empty.pk).exists())

    def test_current_selected_person_is_preserved_with_earlier_person_in_history(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        selected = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.other,
                                    execution_number=2)
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        context = text_detail_context(user=self.admin, text=self.text)
        member = next(row for row in context['team_members'] if row['role'] == 'verifier_1')
        self.assertEqual(member['user']['pk'], self.other.pk)
        self.assertEqual(len([row for row in context['team_members'] if row['role'] == 'verifier_1']), 1)
        self.assertEqual(context['previous_team_members'][0]['user']['pk'], self.member.pk)
        self.assertTrue(A.objects.filter(pk=selected.pk, is_current=True).exists())

    def test_historical_supplement_does_not_replace_an_existing_empty_current_role(self):
        self.screenshot_state()
        A.objects.filter(text=self.text, role='verifier_1').update(execution_number=2)
        current = A.objects.create(text=self.text, role='verifier_1', execution_number=1)
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        self.assertTrue(A.objects.filter(pk=current.pk, is_current=True, assigned_to__isnull=True).exists())

    def test_current_historical_supplement_is_not_promoted_to_a_live_assignment(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        S.objects.filter(pk=verified.pk).update(is_current=True)
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        verifier.refresh_from_db()
        self.assertFalse(verifier.is_current)

    def test_restoration_protects_distinct_verifiers(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        A.objects.create(text=self.text, role='verifier_2', assigned_to=self.member)
        report = restore(apps, 'default')
        self.assertEqual(report['restored'], 0)
        self.assertEqual(len(report['skipped']), 1)
        verifier.refresh_from_db()
        self.assertFalse(verifier.is_current)

    def test_repetition_snapshots_and_handoffs_are_preserved(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        run = R.objects.create(text=self.text, previous_assignment_ids=[verifier.pk])
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        R.objects.filter(pk=run.pk).update(previous_assignment_ids=[], previous_stage_ids=[verified.pk])
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        R.objects.filter(pk=run.pk).update(previous_stage_ids=[])
        A.objects.filter(pk=verifier.pk).update(repetition=run)
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        A.objects.filter(pk=verifier.pk).update(repetition=None)
        replacement = A.objects.create(text=self.text, role='verifier_1', execution_number=2, is_current=False)
        H.objects.create(text=self.text, stage=verified, previous_assignment=verifier,
                          new_assignment=replacement, actor=self.admin, reason='Test')
        self.assertEqual(restore(apps, 'default')['restored'], 0)

    def test_old_cycles_and_terminal_status_are_untouched(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        S.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        S.objects.filter(text=self.text, stage_type='withdrawn').delete()
        self.text.current_workflow_cycle = 2
        self.text.save()
        self.assertEqual(restore(apps, 'default')['restored'], 0)
        verifier.refresh_from_db()
        self.assertFalse(verifier.is_current)


class RetiredMergeMigrationTests(StatusAssignmentFixtures):
    def test_retired_merge_migration_preserves_every_assignment_and_stage(self):
        """Regresja: dawna migracja 0013 scalała prawdziwe kolejne wykonania tej samej roli.

        Obejmuje kilka wykonań z różnymi osobami, pusty bieżący przydział obok historycznego
        z importu oraz drugie wykonanie utworzone przez powtórzenie etapu."""
        for assignment in (
            A.objects.create(text=self.text, role='editor', assigned_to=self.member),
            A.objects.create(text=self.text, role='editor', assigned_to=self.other,
                             execution_number=2, is_current=False),
            A.objects.create(text=self.text, role='editor', assigned_to=self.member,
                             execution_number=3, is_current=False),
        ):
            S.objects.create(text=self.text, stage_type='editing', assignment=assignment,
                             started_at=self.today, ended_at=self.today,
                             execution_number=assignment.execution_number,
                             iteration=assignment.execution_number,
                             is_completed=True, is_current=False)
        imported = Text.objects.create(title='Import', length=100, anthology=self.book)
        A.objects.create(text=imported, role='verifier_1')
        old = A.objects.create(text=imported, role='verifier_1', assigned_to=self.member,
                               execution_number=2, is_current=False)
        with importing():
            S.objects.create(text=imported, stage_type='first_verification', assignment=old,
                             is_completed=True, imported_completed=True, is_current=False,
                             is_released=False)
        repeated = Text.objects.create(title='Powtórzenie', length=100, anthology=self.book)
        for user in (self.member, self.other):
            add_roles(user, 'Redaktor')
        editor = A.objects.create(text=repeated, role='editor', assigned_to=self.member)
        S.objects.create(text=repeated, stage_type='editing', assignment=editor,
                         started_at=self.today - timedelta(days=3))
        repeat_stages(repeated, ['editing'], self.admin, assignees={'editor': self.other})

        def snapshot():
            return (list(A.objects.order_by('pk').values()), list(S.objects.order_by('pk').values()))

        before = snapshot()
        migration = import_module(
            'workflow.migrations.0013_merge_duplicate_role_assignments'
        ).Migration('0013_merge_duplicate_role_assignments', 'workflow')
        migration.apply(ProjectState.from_apps(apps), None)
        self.assertEqual(snapshot(), before)
        state = MigrationExecutor(connection).loader.project_state(
            [('workflow', '0012_restore_retired_team_assignments')])
        migration.apply(state.clone(), None)
        migration.unapply(state.clone(), None)
        self.assertEqual(snapshot(), before)
        self.assertEqual(A.objects.filter(text=repeated, role='editor').count(), 2)
