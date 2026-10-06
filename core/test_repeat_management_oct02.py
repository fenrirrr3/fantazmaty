from datetime import timedelta

from django.core.exceptions import ValidationError
from django.urls import reverse

from core.edit_versions import version_of
from core.forms import RestartWorkflowForm
from core.services.texts import start_assigned_stage
from core.test_status_assignment_regression import StatusAssignmentFixtures
from people.models import Role
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages
from workflow.services import complete_stage


class RepeatManagementOctoberTests(StatusAssignmentFixtures):
    def setUp(self):
        super().setUp()
        for user in (self.member, self.other):
            for name in ('Redaktor', 'Weryfikator', 'Korektor'):
                user.person_profile.roles.add(Role.objects.get_or_create(name=name)[0])
        self.editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.editing = S.objects.create(text=self.text, stage_type='editing', assignment=self.editor,
                                        started_at=self.today - timedelta(days=3))

    def change(self, selected=None, assignees=None, **kwargs):
        return repeat_stages(self.text, selected or ['editing'], self.admin,
                             assignees=assignees or {'editor': self.other}, **kwargs)

    def test_change_finishes_current_work_and_creates_second_execution_for_new_person(self):
        original_start = self.editing.started_at
        run = self.change()
        self.editing.refresh_from_db()
        self.editor.refresh_from_db()
        new = run.stages.get()
        self.assertTrue(self.editing.is_completed)
        self.assertFalse(self.editing.is_current)
        self.assertEqual(self.editing.started_at, original_start)
        self.assertEqual(self.editing.ended_at, self.today)
        self.assertEqual(self.editing.assignment_id, self.editor.pk)
        self.assertEqual(self.editor.assigned_to_id, self.member.pk)
        self.assertFalse(self.editor.is_current)
        self.assertEqual(new.execution_number, 2)
        self.assertEqual(new.assignment.execution_number, 2)
        self.assertEqual(new.assignment.assigned_to_id, self.other.pk)
        self.assertIsNone(new.started_at)
        self.assertTrue(new.is_released)
        self.assertFalse(new.is_completed)

    def test_archived_checkpoint_can_be_selected_while_editing_is_open(self):
        first = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        verified = S.objects.create(text=self.text, stage_type='first_verification', assignment=first,
                    started_at=self.today - timedelta(days=5), ended_at=self.today - timedelta(days=4),
                    is_completed=True, is_current=False, is_released=False)
        run = self.change(['first_verification'], {'verifier_1': self.other})
        self.editing.refresh_from_db()
        verified.refresh_from_db()
        self.assertTrue(self.editing.is_completed)
        self.assertFalse(self.editing.is_current)
        self.assertEqual(verified.ended_at, self.today - timedelta(days=4))
        self.assertEqual(run.stages.get().assignment.assigned_to_id, self.other.pk)
        self.assertEqual(run.stages.get().execution_number, 2)

    def test_unused_reservation_is_retired_without_fake_completed_verification(self):
        reserved = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.admin)
        pending = S.objects.create(text=self.text, stage_type='first_verification', assignment=reserved)
        self.change()
        pending.refresh_from_db()
        self.assertFalse(pending.is_completed)
        self.assertFalse(pending.is_current)
        self.assertFalse(pending.is_released)
        self.assertIsNone(pending.ended_at)

    def test_future_booking_keeps_dates_in_history_without_fake_completion(self):
        self.editing.started_at = self.today + timedelta(days=2)
        self.editing.save()
        self.change()
        self.editing.refresh_from_db()
        self.assertFalse(self.editing.is_current)
        self.assertFalse(self.editing.is_completed)
        self.assertIsNone(self.editing.ended_at)
        self.assertEqual(self.editing.started_at, self.today + timedelta(days=2))

    def test_same_person_is_rejected_before_closing_current_work(self):
        with self.assertRaises(ValidationError):
            self.change(assignees={'editor': self.member})
        self.editing.refresh_from_db()
        self.assertTrue(self.editing.is_current)
        self.assertFalse(self.editing.is_completed)
        self.assertEqual(A.objects.filter(text=self.text).count(), 1)

    def test_stale_preview_and_second_open_queue_do_not_change_data(self):
        old_version = version_of(self.text)
        self.editing.save()
        with self.assertRaises(ValidationError):
            self.change(expected_version=old_version)
        self.change()
        before = list(S.objects.filter(text=self.text).values())
        with self.assertRaises(ValidationError):
            self.change(assignees={'editor': self.admin})
        self.assertEqual(before, list(S.objects.filter(text=self.text).values()))

    def test_form_requires_new_person_and_exposes_searchable_active_stage(self):
        form = RestartWorkflowForm(text=self.text)
        self.assertIn(('editing', 'Redakcja'), form.fields['stages'].choices)
        self.assertEqual(form.fields['performer_editor'].widget.attrs['data-searchable-person'], 'true')
        self.assertFalse(form.fields['performer_editor'].queryset.filter(pk=self.member.pk).exists())
        self.assertTrue(form.fields['performer_editor'].queryset.filter(pk=self.other.pk).exists())
        invalid = RestartWorkflowForm({'stages': ['editing']}, text=self.text)
        self.assertFalse(invalid.is_valid())
        self.assertIn('performer_editor', invalid.errors)

    def test_signed_preview_preserves_people_and_rejects_changed_person(self):
        self.client.force_login(self.admin)
        url = reverse('core:restart_text_workflow', args=[self.text.pk])
        data = {'stages': ['editing'], 'performer_editor': self.other.pk}
        preview = self.client.post(url, data)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Anna Test')
        self.editing.refresh_from_db()
        self.assertFalse(self.editing.is_completed)
        token = preview.context['token']
        changed = dict(data, token=token, confirm_restart='yes', performer_editor=self.admin.pk)
        self.assertEqual(self.client.post(url, changed).status_code, 302)
        self.assertFalse(self.text.repetitions.exists())
        self.assertEqual(self.client.post(url, dict(data, token=token, confirm_restart='yes')).status_code, 302)
        self.assertEqual(self.text.repetitions.get().assignments.get().assigned_to_id, self.other.pk)

    def test_ready_anthology_is_reopened_and_new_performer_can_work(self):
        self.editing.ended_at = self.today
        self.editing.is_completed = True
        self.editing.save()
        S.objects.create(text=self.text, stage_type='ready')
        self.book.status = 'ready'
        self.book.save()
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Zarządzanie etapami')
        self.assertNotContains(response, 'Pokaż kolejkę powtórzeń')
        self.assertContains(response, reverse('admin:texts_text_change', args=[self.text.pk]))
        run = self.change()
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'in_preparation')
        new = run.stages.get()
        start_assigned_stage(user=self.other, stage_id=new.pk, started_at=self.today)
        new.refresh_from_db()
        self.assertEqual(new.started_at, self.today)
        complete_stage(new, self.other, self.today)
        run.refresh_from_db()
        self.assertIsNotNone(run.completed_at)

    def test_unselected_old_editor_history_keeps_dates_and_person(self):
        old = S.objects.create(text=self.text, stage_type='author_editing', assignment=self.editor,
                    started_at=self.today - timedelta(days=5), ended_at=self.today - timedelta(days=4),
                    is_completed=True, is_current=False)
        self.change()
        old.refresh_from_db()
        self.assertEqual(old.assignment_id, self.editor.pk)
        self.assertEqual(old.ended_at, self.today - timedelta(days=4))

    def test_finishing_second_editing_execution_continues_to_verification_not_ready(self):
        run = self.change()
        stage = run.stages.get()
        start_assigned_stage(user=self.other, stage_id=stage.pk, started_at=self.today)
        complete_stage(stage, self.other, self.today)
        self.assertFalse(S.objects.current_cycle().filter(text=self.text, stage_type='ready').exists())
        self.assertTrue(S.objects.current_cycle().filter(text=self.text, stage_type='first_verification',
                                                       is_completed=False).exists())

    def test_finishing_repeated_first_verification_returns_to_editor_and_offers_second(self):
        from core.selectors.texts import text_detail_context
        first = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='first_verification', assignment=first,
                         started_at=self.today, ended_at=self.today, is_completed=True, is_current=False)
        run = self.change(['first_verification'], {'verifier_1': self.other})
        stage = run.stages.get()
        start_assigned_stage(user=self.other, stage_id=stage.pk, started_at=self.today)
        complete_stage(stage, self.other, self.today)
        editing = S.objects.current_cycle().get(text=self.text, stage_type='editing', is_completed=False)
        start_assigned_stage(user=self.member, stage_id=editing.pk, started_at=self.today)
        context = text_detail_context(user=self.member, text=self.text)
        self.assertFalse(context['can_send_to_first_verification'])
        self.assertTrue(context['can_send_to_second_verification'])

    def test_new_executions_can_swap_verifiers_without_overwriting_old_people(self):
        old = {}
        for kind, role, person in (('first_verification', 'verifier_1', self.member),
                                   ('second_verification', 'verifier_2', self.other)):
            old[role] = A.objects.create(text=self.text, role=role, assigned_to=person)
            S.objects.create(text=self.text, stage_type=kind, assignment=old[role],
                             started_at=self.today, ended_at=self.today, is_completed=True, is_current=False)
        run = self.change(['first_verification', 'second_verification'],
                          {'verifier_1': self.other, 'verifier_2': self.member})
        self.assertEqual(run.assignments.get(role='verifier_1').assigned_to_id, self.other.pk)
        self.assertEqual(run.assignments.get(role='verifier_2').assigned_to_id, self.member.pk)
        old['verifier_1'].refresh_from_db()
        self.assertEqual(old['verifier_1'].assigned_to_id, self.member.pk)

    def test_real_second_execution_survives_retired_merge_migration(self):
        from importlib import import_module
        from django.apps import apps
        from django.db.migrations.state import ProjectState
        self.change()
        before = list(A.objects.filter(text=self.text).order_by('pk').values())
        migration = import_module(
            'workflow.migrations.0013_merge_duplicate_role_assignments'
        ).Migration('0013_merge_duplicate_role_assignments', 'workflow')
        migration.apply(ProjectState.from_apps(apps), None)
        self.assertEqual(list(A.objects.filter(text=self.text).order_by('pk').values()), before)
        self.assertEqual(A.objects.filter(text=self.text, role='editor').count(), 2)
