"""Regressions for editorial ownership, verification gates and detail claims."""
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.selectors.texts import my_texts_context, text_detail_context, workflow_list_context
from people.models import Vacation
from texts.models import Anthology, Text
from workflow.availability import claim_access
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.read_queries import annotate_my_work, available_stages
from workflow.repetitions import repeat_stages
from workflow.services import (
    claim_stage, complete_stage, finish_editing_to_coordinator, resume_editing,
    send_to_first_verification, send_to_second_verification, start_first_verification,
)
from workflow.tests import WorkflowTestDataMixin


class EditorialVisibilityTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        override.enable()
        self.addCleanup(override.disable)
        self.text.anthology = Anthology.objects.create(title='Antologia')
        self.text.save()

    def my_ids(self, user=None, view='active'):
        return [r['pk'] for r in my_texts_context(user=user or self.editor, selected_view=view)['texts']]

    def editor_work(self):
        return annotate_my_work(Text.objects.all(), self.editor, self.today).get(pk=self.text.pk)

    def detail(self, user=None):
        self.client.force_login(user or self.editor)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def claim_form(self, response, stage):
        forms = html.fromstring(response.content).xpath('//form[@action=$url]',
            url=reverse('core:take_workflow_stage', args=[stage.pk]))
        self.assertEqual(len(forms), 1)
        return forms[0]

    def test_editor_finishes_on_approved_handoff_to_proofreading(self):
        self.begin_editing()
        send_to_first_verification(self.text, self.editor, self.today)
        self.assertIn(self.text.pk, self.my_ids())
        self.assertTrue(self.editor_work().work_waiting)
        first = claim_stage(self.text, 'first_verification', self.verifier_1)
        self.assertIn(self.text.pk, self.my_ids())
        complete_stage(first, self.verifier_1, self.today)
        resume_editing(self.text, self.editor, self.today)
        self.assertTrue(self.editor_work().work_active)
        send_to_second_verification(self.text, self.editor, self.today)
        second = claim_stage(self.text, 'second_verification', self.verifier_2)
        complete_stage(second, self.verifier_2, self.today)
        resume_editing(self.text, self.editor, self.today)
        finish_editing_to_coordinator(self.text, self.editor, self.today)
        control = claim_stage(self.text, 'editing_control', self.coordinator)
        self.assertIn(self.text.pk, self.my_ids())
        complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        self.assertNotIn(self.text.pk, self.my_ids())
        self.assertTrue(self.editor_work().work_completed)
        proof = claim_stage(self.text, 'first_proofreading', self.proofreader,
                            started_at=self.today + timedelta(days=1))
        self.assertNotIn(self.text.pk, self.my_ids())
        S.objects.filter(pk=proof.pk).update(started_at=self.today)
        self.assertNotIn(self.text.pk, self.my_ids())
        self.assertIn(self.text.pk, self.my_ids(view='all'))
        self.assertTrue(self.editor_work().work_completed)
        complete_stage(S.objects.get(pk=proof.pk), self.proofreader, self.today)
        self.assertTrue(self.editor_work().work_completed)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='editing').count(), 3)

    def test_imported_editorial_history_does_not_hide_current_editor_waiting_for_verification(self):
        editing = self.begin_editing()
        token = importing_completed.set(True)
        try:
            S.objects.create(text=self.text, stage_type='editing', iteration=2,
                assignment=editing.assignment, is_current=False, imported_completed=True, is_completed=True)
        finally:
            importing_completed.reset(token)
        send_to_first_verification(self.text, self.editor, self.today)
        self.assertTrue(self.editor_work().work_waiting)
        self.assertIn(self.text.pk, self.my_ids())
        self.client.force_login(self.editor)
        page = self.client.get(reverse('core:my_texts'))
        self.assertContains(page, self.text.title)
        self.assertContains(page, 'Oczekujące')

    def test_pending_task_is_in_progress_and_old_tabs_resolve_to_remaining_tabs(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(started_at=self.today + timedelta(days=1))
        self.assertIn(self.text.pk, self.my_ids())
        self.client.force_login(self.editor)
        for view, selected in [('active', 'active'), ('waiting', 'active'), ('completed', 'all')]:
            with self.subTest(view=view):
                page = self.client.get(reverse('core:my_texts'), {'view': view})
                self.assertEqual(page.context['selected_view'], selected)
                nav = html.fromstring(page.content).xpath('//nav[@aria-label="Widok moich tekstów"]')[0]
                self.assertEqual([''.join(a.itertext()).strip() for a in nav.xpath('./a')], ['W toku', 'Wszystkie'])
                self.assertContains(page, 'Oczekujące')

    def test_ordinary_completed_task_only_remains_in_all(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.proofreader)
        S.objects.create(text=self.text, stage_type='first_proofreading', assignment=assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertNotIn(self.text.pk, self.my_ids(self.proofreader))
        self.assertIn(self.text.pk, self.my_ids(self.proofreader, 'all'))

    def test_proofreading_from_previous_cycle_does_not_finish_current_editorial_work(self):
        self.begin_editing()
        S.objects.create(text=self.text, stage_type='first_proofreading', workflow_cycle=2,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        send_to_first_verification(self.text, self.editor, self.today)
        self.assertTrue(self.editor_work().work_waiting)

    def test_old_proofreading_does_not_approve_editorial_repetition(self):
        self.begin_editing()
        S.objects.create(text=self.text, stage_type='first_proofreading',
                         started_at=self.today, ended_at=self.today, is_completed=True)
        run = repeat_stages(self.text, ['editing'], self.superuser,
                            assignees={'editor': self.other_editor})
        self.assertIn(self.text.pk, self.my_ids(self.other_editor))
        row = annotate_my_work(Text.objects.all(), self.other_editor, self.today).get(pk=self.text.pk)
        self.assertTrue(row.work_waiting)
        self.assertFalse(row.work_completed)
        self.assertIsNone(run.stages.get().started_at)

    def test_preassigned_verifier_keeps_first_button_and_blocks_second_before_completion(self):
        editing = self.begin_editing()
        first = claim_stage(self.text, 'first_verification', self.verifier_1)
        response = self.detail()
        self.assertContains(response, 'Przekaż do pierwszej weryfikacji')
        self.assertNotContains(response, 'Przekaż do drugiej weryfikacji')
        with self.assertRaisesMessage(ValidationError, 'Najpierw należy zakończyć pierwszą'):
            send_to_second_verification(self.text, self.editor, self.today)
        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)
        send_to_first_verification(self.text, self.editor, self.today)
        first = start_first_verification(self.text, self.verifier_1, self.today)
        complete_stage(first, self.verifier_1, self.today)
        resume_editing(self.text, self.editor, self.today)
        response = self.detail()
        self.assertNotContains(response, 'Przekaż do pierwszej weryfikacji')
        self.assertContains(response, 'Przekaż do drugiej weryfikacji')

    def test_detail_claims_same_stage_as_available_list_and_submits_with_version(self):
        response = self.detail(self.editor)
        form = self.claim_form(response, self.initial_stage)
        self.assertEqual([r['pk'] for r in response.context['claimable_stages']],
            list(available_stages(self.editor, claim_access(self.editor)).filter(text=self.text).values_list('pk', flat=True)))
        data = {node.get('name'): node.get('value', '') for node in form.xpath('.//input[@name]')}
        self.assertIn('_edit_version', data)
        self.assertEqual(data['started_at'], self.today.isoformat())
        taken = self.client.post(form.get('action'), data)
        self.assertRedirects(taken, reverse('core:assigned_text_detail', args=[self.text.pk]), fetch_redirect_response=False)
        self.assertEqual(A.objects.current_cycle().get(text=self.text, role='editor').assigned_to, self.editor)
        self.assertEqual(self.stage('editing').started_at, self.today)
        self.assertFalse(self.detail().context['claimable_stages'])

    def test_detail_verification_claim_reserves_without_start_date(self):
        self.begin_editing()
        first = self.stage('first_verification')
        response = self.detail(self.verifier_1)
        form = self.claim_form(response, first)
        self.assertFalse(form.xpath('.//input[@name="started_at"]'))
        data = {node.get('name'): node.get('value', '') for node in form.xpath('.//input[@name]')}
        self.client.post(form.get('action'), data)
        first.refresh_from_db()
        self.assertEqual(first.assignment.assigned_to, self.verifier_1)
        self.assertIsNone(first.started_at)
        self.assertFalse(self.detail(self.verifier_2).context['claimable_stages'])

    def test_claim_controls_are_hidden_for_wrong_role_leave_and_closed_anthology(self):
        self.assertFalse(text_detail_context(user=self.proofreader, text=self.text)['claimable_stages'])
        Vacation.objects.create(person=self.editor.person_profile, start_date=self.today, until_revoked=True)
        self.assertFalse(text_detail_context(user=self.editor, text=self.text)['claimable_stages'])
        # Older imports may contain a closed anthology with an open text.
        Anthology.objects.filter(pk=self.text.anthology_id).update(status='ready')
        self.assertFalse(text_detail_context(user=self.other_editor, text=self.text)['claimable_stages'])

    def test_summary_uses_same_proofreading_gate_as_my_texts(self):
        self.begin_editing()
        send_to_first_verification(self.text, self.editor, self.today)
        S.objects.create(text=self.text, stage_type='editing_control',
                         started_at=self.today, ended_at=self.today, is_completed=True)
        def state():
            row = list(workflow_list_context(user=self.superuser, params={})['stages'])[0]
            return next(cell for cell in row['role_cells'] if cell['role'] == 'editor')['entries'][0]['state']
        self.assertEqual(state(), 'Oczekuje')
        S.objects.create(text=self.text, stage_type='first_proofreading', started_at=self.today)
        self.assertEqual(state(), 'Zakończone')

    def test_first_proofreading_can_be_taken_from_detail_and_finishes_editorial_waiting(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(ended_at=self.today, is_completed=True)
        S.objects.filter(text=self.text, stage_type='first_verification').delete()
        S.objects.create(text=self.text, stage_type='editing_control', started_at=self.today, ended_at=self.today, is_completed=True, send_to_proofreading=True)
        proof = S.objects.create(text=self.text, stage_type='first_proofreading')
        self.assertNotIn(self.text.pk, self.my_ids())
        response = self.detail(self.proofreader)
        form = self.claim_form(response, proof)
        data = {node.get('name'): node.get('value', '') for node in form.xpath('.//input[@name]')}
        taken = self.client.post(form.get('action'), data)
        self.assertRedirects(taken, reverse('core:assigned_text_detail', args=[self.text.pk]), fetch_redirect_response=False)
        proof.refresh_from_db()
        self.assertEqual(proof.assignment.assigned_to, self.proofreader)
        self.assertEqual(proof.started_at, self.today)
        self.assertNotIn(self.text.pk, self.my_ids())
        self.assertTrue(self.editor_work().work_completed)
