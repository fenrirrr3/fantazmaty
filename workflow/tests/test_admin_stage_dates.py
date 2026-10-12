"""Daty etapów w adminie: ręczne rozpoczęcie i zakończenie, przejścia redakcji, historia importu i kolejność etapów;
lista etapów i przypisań w adminie (filtry, akcje zbiorcze, korekta dat zaimportowanej pracy)."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.models import WorkflowEvent
from core.selectors.texts import text_detail_context
from core.services.texts import start_assigned_stage
from fantazmaty.testing import create_member, create_superuser, form_value
from texts.models import Anthology, Text
from workflow.admin_stage_dates import StageDatesForm, preserve_stage_order, set_stage_dates
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.repetitions import repeat_stages
from workflow.services import user_can_complete_stage

from .support import add_roles, importing


class AdminStageDatesTests(TestCase):
    """Formularz „Ustaw daty / zakończ etap” i set_stage_dates dla trwającej pierwszej korekty."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.start = cls.today - timedelta(days=10)
        cls.end = cls.today - timedelta(days=2)
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Korektor')
        add_roles(cls.member, 'Redaktor', 'Weryfikator')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.assignment = A.objects.create(text=cls.text, role='proofreader_1', assigned_to=cls.member)
        cls.stage = S.objects.create(text=cls.text, assignment=cls.assignment,
                                     stage_type='first_proofreading', started_at=cls.start)

    def setUp(self):
        self.client.force_login(self.admin)

    def save_dates(self, **kwargs):
        data = {'started_at': self.start, 'ended_at': None, **kwargs}
        return set_stage_dates(self.stage.pk, self.admin, version_of(self.text), **data)

    def test_manual_start_correction_keeps_execution_performer_and_status(self):
        corrected = self.start + timedelta(days=1)
        self.save_dates(started_at=corrected)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, corrected)
        self.assertFalse(self.stage.is_completed)
        self.assertIsNone(self.stage.ended_at)
        self.assertEqual(self.stage.assignment_id, self.assignment.pk)
        self.assertEqual(self.stage.execution_number, 1)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_finish_uses_manual_dates_creates_next_and_records_actual_admin(self):
        self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.ended_at, self.end)
        pending = S.objects.current_cycle().get(text=self.text, stage_type='second_proofreading')
        self.assertIsNone(pending.started_at)
        self.assertFalse(pending.is_completed)
        event = WorkflowEvent.objects.get(text=self.text)
        self.assertEqual(event.actor_id, self.admin.pk)
        self.assertIn('Zakończono etap', event.details)

    def test_unstarted_work_can_be_started_and_finished_with_past_dates(self):
        self.stage.started_at = None
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)
        self.assertTrue(self.stage.is_completed)

    def test_start_only_accepts_past_date_in_admin(self):
        self.stage.started_at = None
        self.stage.save()
        self.save_dates()
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_past_date_override_is_unavailable_to_regular_member(self):
        self.stage.started_at = None
        self.stage.save()
        with self.assertRaises(PermissionDenied):
            start_assigned_stage(user=self.member, stage_id=self.stage.pk,
                                 started_at=self.start, allow_past=True)
        with self.assertRaises(ValidationError):
            start_assigned_stage(user=self.member, stage_id=self.stage.pk, started_at=self.start)
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.started_at)

    def test_invalid_completion_rolls_back_start_correction_and_successor(self):
        for end in (self.start - timedelta(days=1), self.today + timedelta(days=1)):
            with self.subTest(end=end), self.assertRaises(ValidationError):
                self.save_dates(started_at=self.start + timedelta(days=1), ended_at=end, finish=True)
            self.stage.refresh_from_db()
            self.assertEqual(self.stage.started_at, self.start)
            self.assertFalse(self.stage.is_completed)
            self.assertEqual(S.objects.filter(text=self.text).count(), 1)
            self.assertFalse(WorkflowEvent.objects.filter(text=self.text).exists())

    def test_end_without_transition_and_missing_dates_are_rejected(self):
        for data in ({'ended_at': self.end}, {'finish': True},
                     {'started_at': None, 'ended_at': self.end, 'finish': True}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                self.save_dates(**data)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_stale_and_duplicate_completions_do_not_create_another_successor(self):
        version = version_of(self.text)
        self.save_dates(ended_at=self.end, finish=True)
        with self.assertRaises(ValidationError):
            set_stage_dates(self.stage.pk, self.admin, version, started_at=self.start,
                            ended_at=self.end, finish=True)
        with self.assertRaises(ValidationError):
            self.save_dates(ended_at=self.end, finish=True)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='second_proofreading').count(), 1)

    def test_permission_closed_anthology_and_withdrawal_block_changes(self):
        with self.assertRaises(PermissionDenied):
            set_stage_dates(self.stage.pk, self.member, version_of(self.text), started_at=self.start)
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        marker = S.objects.create(text=self.text, stage_type='ready')
        self.book.status = Anthology.Status.READY
        self.book.save()
        with self.assertRaisesMessage(ValidationError, 'Antologia jest gotowa'):
            self.save_dates(ended_at=self.end, finish=True)
        self.book.status = Anthology.Status.IN_PREPARATION
        self.book.save()
        marker.delete()
        self.stage.is_completed = False
        self.stage.ended_at = None
        self.stage.save()
        S.objects.create(text=self.text, stage_type='withdrawn')
        with self.assertRaises(ValidationError):
            self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_archived_unfinished_stage_cannot_be_finished(self):
        self.stage.is_current = False
        self.stage.save()
        with self.assertRaises(ValidationError):
            self.save_dates(ended_at=self.end, finish=True)

    def test_completed_history_date_correction_does_not_create_work(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.is_current = False
        self.stage.save()
        corrected = self.end - timedelta(days=1)
        self.save_dates(ended_at=corrected)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertFalse(self.stage.is_current)
        self.assertEqual(self.stage.ended_at, corrected)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_imported_history_allows_unknown_dates_and_later_filling_them(self):
        with importing():
            self.stage.imported_completed = True
            self.stage.is_completed = True
            self.stage.started_at = None
            self.stage.save()
        self.save_dates(started_at=None)
        self.save_dates(ended_at=self.end)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed and self.stage.imported_completed)
        self.assertEqual(self.stage.ended_at, self.end)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_start_cannot_precede_previous_completed_work(self):
        S.objects.create(text=self.text, stage_type='editing_control', started_at=self.start,
                         ended_at=self.start + timedelta(days=2), is_completed=True)
        with self.assertRaises(ValidationError):
            self.save_dates(started_at=self.start + timedelta(days=1))
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)

    def test_admin_links_and_date_form_post(self):
        url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])
        for route, pk in (('admin:texts_text_change', self.text.pk),
                          ('admin:workflow_workflowstage_change', self.stage.pk),
                          ('admin:workflow_workflowstage_changelist', None)):
            response = self.client.get(reverse(route, args=[pk] if pk else None))
            self.assertContains(response, url)
            self.assertContains(response, 'Ustaw daty / zakończ etap')
        response = self.client.get(url)
        self.assertContains(response, 'type="date"')
        self.assertContains(response, self.start.isoformat())
        response = self.client.post(url, {'started_at': self.start.isoformat(),
            'ended_at': self.end.isoformat(), 'finish': 'on', 'version': version_of(self.text)})
        self.assertRedirects(response, reverse('admin:texts_text_change', args=[self.text.pk]),
                             fetch_redirect_response=False)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)

    def test_admin_form_displays_errors_without_partial_changes(self):
        url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])
        response = self.client.post(url, {'started_at': self.start.isoformat(),
            'ended_at': (self.start - timedelta(days=1)).isoformat(),
            'finish': 'on', 'version': version_of(self.text)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Data zakończenia nie może być wcześniejsza')
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_editor_control_can_be_started_and_completed_by_admin_for_assigned_editor(self):
        self.stage.stage_type = 'editor_control'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.started_at = None
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.assignment.assigned_to_id, self.member.pk)
        self.assertTrue(S.objects.filter(text=self.text, stage_type='third_proofreading').exists())

    def test_editor_control_still_rejects_unassigned_coordinator(self):
        self.stage.stage_type = 'editor_control'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        other = create_member('other', 'Koordynator')
        self.assertFalse(user_can_complete_stage(self.stage, other))

    def test_normal_editing_transitions_use_existing_checkpoints(self):
        for destination in ('first_verification', 'author_editing', 'second_verification', 'editing_control'):
            with self.subTest(destination=destination):
                text = Text.objects.create(title=destination, length=100, anthology=self.book)
                assignment = A.objects.create(text=text, role='editor', assigned_to=self.member)
                for checkpoint in (['first_verification'] if destination in ('author_editing', 'second_verification')
                                   else ['first_verification', 'second_verification'] if destination == 'editing_control' else []):
                    S.objects.create(text=text, stage_type=checkpoint, started_at=self.start,
                                     ended_at=self.start, is_completed=True)
                editing = S.objects.create(text=text, stage_type='editing', assignment=assignment, started_at=self.start)
                set_stage_dates(editing.pk, self.admin, version_of(text), started_at=self.start,
                                ended_at=self.end, finish=True, next_stage=destination)
                editing.refresh_from_db()
                self.assertTrue(editing.is_completed)
                self.assertEqual(editing.ended_at, self.end)
                self.assertTrue(S.objects.current_cycle().filter(text=text, stage_type=destination,
                                                                 is_completed=False).exists())

    def test_invalid_editing_checkpoint_rolls_back_date_change(self):
        self.stage.stage_type = 'editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        with self.assertRaises(ValidationError):
            self.save_dates(started_at=self.start + timedelta(days=1), ended_at=self.end,
                            finish=True, next_stage='editing_control')
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.stage.started_at, self.start)

    def imported_editing_checkpoints(self, *kinds):
        self.stage.stage_type = 'editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        imported = []
        with importing():
            for kind in kinds:
                role = 'verifier_1' if kind == 'first_verification' else 'verifier_2'
                performer = self.member if role == 'verifier_1' else self.admin
                assignment = A.objects.create(text=self.text, role=role, assigned_to=performer)
                stage = S(text=self.text, stage_type=kind, assignment=assignment,
                          is_completed=True, imported_completed=True)
                stage.full_clean()
                stage.save()
                imported.append(stage)
        return imported

    def test_dateless_completed_first_verification_is_not_offered_again(self):
        first, = self.imported_editing_checkpoints('first_verification')
        form = StageDatesForm(stage=self.stage)
        choices = dict(form.fields['next_stage'].choices)
        self.assertNotIn('first_verification', choices)
        self.assertIn('author_editing', choices)
        self.assertIn('second_verification', choices)
        self.assertNotIn('editing_control', choices)
        detail = text_detail_context(user=self.admin, text=self.text)
        self.assertFalse(detail['can_send_to_first_verification'])
        self.assertTrue(detail['can_send_to_second_verification'])
        with self.assertRaisesMessage(ValidationError, 'Pierwsza weryfikacja jest już zakończona'):
            self.save_dates(ended_at=self.end, finish=True, next_stage='first_verification')
        self.save_dates(ended_at=self.end, finish=True, next_stage='second_verification')
        first.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.ended_at)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)
        self.assertTrue(S.objects.filter(text=self.text, stage_type='second_verification', is_completed=False).exists())

    def test_dateless_completed_second_verification_unlocks_coordinator(self):
        imported = self.imported_editing_checkpoints('first_verification', 'second_verification')
        form = StageDatesForm(stage=self.stage)
        choices = dict(form.fields['next_stage'].choices)
        self.assertNotIn('first_verification', choices)
        self.assertNotIn('second_verification', choices)
        self.assertIn('editing_control', choices)
        detail = text_detail_context(user=self.admin, text=self.text)
        self.assertTrue(detail['can_finish_editing'])
        self.save_dates(ended_at=self.end, finish=True, next_stage='editing_control')
        self.assertTrue(S.objects.filter(text=self.text, stage_type='editing_control', is_completed=False).exists())
        for stage in imported:
            stage.refresh_from_db()
            self.assertTrue(stage.is_completed)
            self.assertIsNone(stage.started_at)
            self.assertIsNone(stage.ended_at)

    def test_admin_rejects_post_of_completed_dateless_verification(self):
        self.imported_editing_checkpoints('first_verification')
        url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])
        choices = dict(self.client.get(url).context['form'].fields['next_stage'].choices)
        self.assertNotIn('first_verification', choices)
        self.assertIn('second_verification', choices)
        response = self.client.post(url, {'started_at': self.start.isoformat(),
            'ended_at': self.end.isoformat(), 'finish': 'on', 'next_stage': 'first_verification',
            'version': version_of(self.text)})
        self.assertEqual(response.status_code, 200)
        self.assertIn('next_stage', response.context['form'].errors)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)

    def test_retired_verification_counts_but_a_different_cycle_does_not(self):
        first, = self.imported_editing_checkpoints('first_verification')
        first.is_current = False
        first.save()
        choices = dict(StageDatesForm(stage=self.stage).fields['next_stage'].choices)
        self.assertNotIn('first_verification', choices)
        self.assertIn('second_verification', choices)
        first.is_current = True
        first.workflow_cycle = self.text.current_workflow_cycle + 1
        first.save()
        choices = dict(StageDatesForm(stage=self.stage).fields['next_stage'].choices)
        self.assertIn('first_verification', choices)

    def test_author_completion_resumes_editing_on_manual_end_date(self):
        self.stage.stage_type = 'author_editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True)
        editing = S.objects.current_cycle().get(text=self.text, stage_type='editing')
        self.assertEqual(editing.started_at, self.end)
        self.assertFalse(editing.is_completed)

    def test_ready_for_editing_start_becomes_editing_and_can_be_forwarded(self):
        self.stage.stage_type = 'ready_for_editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.started_at = None
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True, next_stage='first_verification')
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.stage_type, 'editing')
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(S.objects.current_cycle().filter(text=self.text, stage_type='first_verification').count(), 1)

    def test_repetition_releases_next_selected_stage_without_regular_successor(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        S.objects.create(text=self.text, stage_type='styling', started_at=self.start,
                         ended_at=self.end, is_completed=True)
        S.objects.create(text=self.text, stage_type='ready', started_at=self.end)
        run = repeat_stages(self.text, ['first_proofreading', 'styling'], self.admin)
        repeated = run.stages.get(stage_type='first_proofreading')
        blocked = run.stages.get(stage_type='styling')
        with self.assertRaises(ValidationError):
            set_stage_dates(blocked.pk, self.admin, version_of(self.text), started_at=self.today,
                            ended_at=self.today, finish=True)
        repeated.assignment.assigned_to = self.member
        repeated.assignment.save()
        set_stage_dates(repeated.pk, self.admin, version_of(self.text), started_at=self.today,
                        ended_at=self.today, finish=True)
        blocked.refresh_from_db()
        self.assertTrue(blocked.is_released)
        self.assertFalse(S.objects.current_cycle().filter(text=self.text, stage_type='second_proofreading').exists())
        blocked.assignment.assigned_to = self.admin
        blocked.assignment.save()
        set_stage_dates(blocked.pk, self.admin, version_of(self.text), started_at=self.today,
                        ended_at=self.today, finish=True)
        run.refresh_from_db()
        self.assertIsNotNone(run.completed_at)
        self.assertTrue(S.objects.current_cycle().filter(text=self.text, stage_type='ready').exists())

    def test_date_correction_cannot_reorder_stages(self):
        text = Text.objects.create(title='Kolejność', length=100, anthology=self.book)
        first = S.objects.create(text=text, stage_type='editing', is_completed=True,
                                 started_at=self.today - timedelta(days=10), ended_at=self.today - timedelta(days=8))
        S.objects.create(text=text, stage_type='first_verification', is_completed=True,
                         started_at=self.today - timedelta(days=7), ended_at=self.today - timedelta(days=5))
        with self.assertRaises(ValidationError):
            preserve_stage_order(first, self.today - timedelta(days=10), self.today - timedelta(days=6))
        preserve_stage_order(first, self.today - timedelta(days=11), self.today - timedelta(days=7))


class AdminStageListTests(TestCase):
    """Lista etapów i przypisań w adminie: filtry aktywnych rodzajów, akcje zbiorcze i ręczna korekta dat."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Korektor')
        cls.today = timezone.localdate()

    def setUp(self):
        self.text = Text.objects.create(title='Tekst', length=100)
        self.assignment = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.member)
        self.stage = S.objects.create(text=self.text, stage_type='first_proofreading', assignment=self.assignment)
        self.client.force_login(self.admin)

    def action(self, name, *stages):
        return self.client.post(reverse('admin:workflow_workflowstage_changelist'),
                                {'action': name, '_selected_action': [s.pk for s in stages]}, follow=True)

    def test_filters_narrow_stage_and_assignment_lists(self):
        S.objects.create(text=Text.objects.create(title='Inny', length=1), stage_type='ready_for_editing')
        stages = self.client.get(reverse('admin:workflow_workflowstage_changelist'), {'stage_type': 'first_proofreading'})
        self.assertEqual(list(stages.context['cl'].result_list), [self.stage])
        roles = self.client.get(reverse('admin:workflow_workflowroleassignment_changelist'), {'role': 'editor'})
        self.assertEqual(list(roles.context['cl'].result_list), [])
        roles = self.client.get(reverse('admin:workflow_workflowroleassignment_changelist'), {'role': 'proofreader_1'})
        self.assertEqual(list(roles.context['cl'].result_list), [self.assignment])

    def test_bulk_start_then_finish_creates_next_stage(self):
        self.assertContains(self.action('start_selected_stages', self.stage), 'Rozpoczęto etapy.')
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.today)
        self.assertContains(self.action('finish_selected_stages', self.stage), 'Zakończono etapy i utworzono następne.')
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertTrue(S.objects.filter(text=self.text, stage_type='second_proofreading').exists())

    def test_bulk_action_error_rolls_back_whole_selection(self):
        other_text = Text.objects.create(title='Drugi', length=100)
        other = S.objects.create(text=other_text, stage_type='first_proofreading',
                                 assignment=A.objects.create(text=other_text, role='proofreader_1',
                                                             assigned_to=self.member))
        S.objects.filter(pk=self.stage.pk).update(started_at=self.today)
        # The first text can finish, the second is not started yet: nothing may be saved.
        response = self.action('finish_selected_stages', self.stage, other)
        self.assertEqual(len([m for m in response.context['messages'] if m.level_tag == 'error']), 1)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertFalse(S.objects.filter(stage_type='second_proofreading').exists())
        unassigned = S.objects.create(text=Text.objects.create(title='Trzeci', length=1), stage_type='first_proofreading')
        response = self.action('start_selected_stages', other, unassigned)
        self.assertNotContains(response, 'Rozpoczęto etapy.')
        other.refresh_from_db()
        self.assertIsNone(other.started_at)

    def test_imported_dates_need_confirmation_and_keep_stage_order(self):
        start = self.today - timedelta(days=10)
        S.objects.create(text=self.text, stage_type='editing', started_at=start, ended_at=start + timedelta(days=2),
                         is_completed=True)
        with importing():
            self.stage.imported_completed = True
            self.stage.is_completed = True
            self.stage.save()
        url = reverse('admin:workflow_workflowstage_change', args=[self.stage.pk])
        page = self.client.get(url)
        data = {'_edit_version': form_value(page), '_save': 'Save',
                'started_at': (start + timedelta(days=4)).isoformat(),
                'ended_at': (start + timedelta(days=5)).isoformat()}
        response = self.client.post(url, data)
        self.assertContains(response, 'Ręczna korekta wymaga potwierdzenia')
        response = self.client.post(url, dict(data, confirm_data_correction='on'))
        self.assertEqual(response.status_code, 302)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, start + timedelta(days=4))
        self.assertTrue(self.stage.is_completed)
        # Known dates cannot be moved before the end of the preceding editing.
        data['_edit_version'] = form_value(self.client.get(url))
        response = self.client.post(url, dict(data, confirm_data_correction='on', started_at=start.isoformat()))
        self.assertContains(response, 'wcześniejsza niż zakończenie poprzedniej pracy')
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, start + timedelta(days=4))
