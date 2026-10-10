from datetime import date
from importlib import import_module
from io import StringIO
import re

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from core.edit_versions import version_of
from core.selectors.texts import text_detail_context
from core.test_status_assignment_regression import StatusAssignmentFixtures
from workflow.models import WorkflowHandoff as H, WorkflowRepetition as R
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.services import send_to_second_verification, resume_editing


old_repair = import_module('workflow.migrations.0011_merge_status_assignments').repair_status_assignments
restore = import_module('workflow.migrations.0012_restore_retired_team_assignments').restore_team_assignments


class RetiredTeamAssignmentTests(StatusAssignmentFixtures):
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

    def test_screenshot_case_restores_team_even_when_0011_finds_zero_duplicates(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        known_date = verifier.assigned_at
        self.assertEqual(old_repair(apps, 'default')['restored'], 0)
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

    def test_template_separates_history_from_current_team_before_repair(self):
        self.screenshot_state()
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        sections = re.findall(r'<details class="text-team"[^>]*>(.*?)</details>', response.content.decode(), re.S)
        current = next(section for section in sections if 'Osoby przypisane do tekstu' in section)
        previous = next(section for section in sections if 'Historia przypisań osób' in section)
        self.assertEqual(current.count('Weryfikator 1'), 1)
        self.assertNotIn('Renata', current)
        self.assertIn('Renata', previous)
        self.assertIn('przebieg 1', previous)

    def test_template_has_one_filled_verifier_card_after_repair(self):
        self.screenshot_state()
        restore(apps, 'default')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        sections = re.findall(r'<details class="text-team"[^>]*>(.*?)</details>', response.content.decode(), re.S)
        current = next(section for section in sections if 'Osoby przypisane do tekstu' in section)
        self.assertEqual(current.count('Weryfikator 1'), 1)
        self.assertIn('Renata', current)
        # The history section is always present; after the repair it has no entries.
        history = next(section for section in sections if 'Historia przypisań osób' in section)
        self.assertIn('Brak wcześniejszych przypisań', history)

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

    def test_preview_preserves_database_and_apply_reports_restored_assignment(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        before = version_of(self.text)
        output = StringIO()
        call_command('repair_status_assignments', stdout=output)
        self.assertIn('Puste duplikaty: 0; przypisania do przywrócenia: 1.', output.getvalue())
        self.assertEqual(version_of(self.text), before)
        verifier.refresh_from_db()
        self.assertFalse(verifier.is_current)
        call_command('repair_status_assignments', apply=True, stdout=StringIO())
        verifier.refresh_from_db()
        self.assertTrue(verifier.is_current)

    def test_text_inspection_reports_exact_assignments_without_changing_them(self):
        editor, editing, verifier, verified, author = self.screenshot_state()
        before = version_of(self.text)
        output = StringIO()
        call_command('repair_status_assignments', inspect_text=self.text.title, stdout=output)
        self.assertIn(f'Przydział {verifier.pk}: verifier_1', output.getvalue())
        self.assertIn(f'Etap {verified.pk}: first_verification', output.getvalue())
        self.assertIn('aktualny=False', output.getvalue())
        self.assertEqual(version_of(self.text), before)
        verifier.refresh_from_db()
        self.assertFalse(verifier.is_current)

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
        self.assertEqual(old_repair(apps, 'default')['merged'], 0)
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
