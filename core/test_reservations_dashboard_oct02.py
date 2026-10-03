"""Actual dateless archived W1 regression, admin reservations and dashboard waits."""
from datetime import timedelta
from io import StringIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command, CommandError
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.selectors.texts import _annotated_texts, text_detail_context, user_workflow_summary, my_texts_context
from core.test_status_assignment_regression import StatusAssignmentFixtures
from people.models import Role
from texts.models import Text
from workflow.admin_add_stage import add_missing_stage
from workflow.admin_stage_edit import edit_stage
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.reservation_repair import restore_first_verification_reservation
from workflow.services import (
    completed_stage_exists, send_to_first_verification, send_to_second_verification,
    start_first_verification, claim_stage, complete_stage, resume_editing,
)


class ReservationAndDashboardTests(StatusAssignmentFixtures):
    def setUp(self):
        super().setUp()
        for person in (self.member.person_profile, self.other.person_profile):
            for name in ('Redaktor', 'Weryfikator', 'Korektor'):
                person.roles.add(Role.objects.get_or_create(name=name)[0])
        self.text.title = 'Zabójczyni Snów'
        self.text.save()
        self.editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.editing = S.objects.create(text=self.text, stage_type='editing', assignment=self.editor,
                                       started_at=self.today - timedelta(days=24))

    def false_completion(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.other, is_current=False)
        token = importing_completed.set(True)
        try:
            return S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment,
                is_current=False, is_released=False, imported_completed=True, is_completed=True)
        finally:
            importing_completed.reset(token)

    def reserve(self, performer=None, **kwargs):
        return add_missing_stage(self.text.pk, self.admin, version_of(self.text),
                                 kind='first_verification', performer=performer or self.other, **kwargs)

    def summary(self, user=None):
        return user_workflow_summary(user or self.member)

    def home(self, user=None):
        self.client.force_login(user or self.member)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        return response

    def close_editing(self):
        S.objects.filter(pk=self.editing.pk).update(is_completed=True, ended_at=self.today)

    def test_screenshot_state_preview_then_repair_changes_second_button_back_to_first(self):
        first = self.false_completion()
        before = text_detail_context(user=self.member, text=self.text)
        self.assertFalse(before['can_send_to_first_verification'])
        self.assertTrue(before['can_send_to_second_verification'])
        version = version_of(self.text)
        out = StringIO()
        call_command('restore_first_verification', text_title=self.text.title, stdout=out)
        first.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertFalse(first.is_current)
        self.assertEqual(version_of(self.text), version)
        self.assertIn('Podgląd bez zapisu', out.getvalue())
        original_assignment = first.assignment_id
        call_command('restore_first_verification', text_title=self.text.title, apply=True, stdout=StringIO())
        first.refresh_from_db()
        self.assertFalse(first.is_completed or first.imported_completed)
        self.assertTrue(first.is_current and first.is_released and first.assignment.is_current)
        self.assertEqual(first.assignment_id, original_assignment)
        self.assertEqual(first.assignment.assigned_to, self.other)
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.ended_at)
        self.assertEqual(S.objects.filter(text=self.text).count(), 2)
        self.assertEqual(A.objects.filter(text=self.text).count(), 2)
        after = text_detail_context(user=self.member, text=self.text)
        self.assertTrue(after['can_send_to_first_verification'])
        self.assertFalse(after['can_send_to_second_verification'])
        with self.assertRaises(ValidationError):
            send_to_second_verification(self.text, self.member, self.today)
        with self.assertRaises(ValidationError):
            start_first_verification(self.text, self.other, self.today)
        send_to_first_verification(self.text, self.member, self.today)
        started = start_first_verification(self.text, self.other, self.today)
        self.assertEqual(started.pk, first.pk)
        complete_stage(started, self.other, self.today)
        resume_editing(self.text, self.member, self.today)
        self.assertTrue(text_detail_context(user=self.member, text=self.text)['can_send_to_second_verification'])

    def test_repair_is_idempotent_and_other_dateless_history_stays_completed(self):
        first = self.false_completion()
        other_text = Text.objects.create(title='W1 rzeczywiście ukończona', anthology=self.book, length=100)
        token = importing_completed.set(True)
        try:
            historical = S.objects.create(text=other_text, stage_type='first_verification',
                                           is_completed=True, imported_completed=True, is_current=False)
        finally:
            importing_completed.reset(token)
        restore_first_verification_reservation(first.pk, apply=True)
        version = version_of(self.text)
        report = restore_first_verification_reservation(first.pk, apply=True)
        self.assertFalse(report['changed'])
        self.assertEqual(version_of(self.text), version)
        historical.refresh_from_db()
        self.assertTrue(historical.is_completed)
        self.assertTrue(completed_stage_exists(other_text, 'first_verification'))

    def test_admin_offers_restoring_archived_reservation_and_saves_without_a_start_date(self):
        first = self.false_completion()
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin:texts_text_change', args=[self.text.pk]))
        self.assertContains(response, '?action=restore_reservation')
        response = self.client.post(reverse('admin:workflow_stage_correct', args=[first.pk]),
            {'action': 'restore_reservation', 'version': version_of(self.text), 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_completed)
        self.assertIsNone(first.started_at)

    def test_non_admin_and_stale_form_cannot_restore_reservation(self):
        first = self.false_completion()
        for actor, version, error in ((self.member, version_of(self.text), PermissionDenied),
                                      (self.admin, version_of(self.text) - 1, ValidationError)):
            with self.assertRaises(error):
                edit_stage(first.pk, actor, version, action='restore_reservation')
        first.refresh_from_db()
        self.assertTrue(first.is_completed)

    def test_repair_rejects_dated_work_further_stages_duplicate_titles_and_previous_cycle(self):
        first = self.false_completion()
        S.objects.filter(pk=first.pk).update(started_at=self.today, ended_at=self.today)
        with self.assertRaises(ValidationError):
            restore_first_verification_reservation(first.pk, apply=True)
        S.objects.filter(pk=first.pk).update(started_at=None, ended_at=None)
        later = S.objects.create(text=self.text, stage_type='second_verification')
        with self.assertRaises(ValidationError):
            restore_first_verification_reservation(first.pk, apply=True)
        later.delete()
        Text.objects.create(title=self.text.title, anthology=self.book, length=100)
        with self.assertRaises(CommandError):
            call_command('restore_first_verification', text_title=self.text.title, apply=True, stdout=StringIO())
        self.text.current_workflow_cycle = 2
        self.text.save()
        with self.assertRaises(ValidationError):
            restore_first_verification_reservation(first.pk, apply=True)
        first.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertFalse(first.is_current)

    def test_admin_adds_first_reservation_during_editing_and_preserves_active_status(self):
        first = self.reserve()
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.ended_at)
        self.assertFalse(first.is_completed or first.imported_completed)
        self.assertEqual(first.assignment.assigned_to, self.other)
        self.editing.refresh_from_db()
        self.assertFalse(self.editing.is_completed)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'editing')
        with self.assertRaises(ValidationError):
            start_first_verification(self.text, self.other, self.today)

    def test_admin_add_form_reuses_pending_first_stage_and_assignment_for_new_person(self):
        first = self.reserve()
        aid = first.assignment_id
        second = self.reserve(performer=self.admin)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.assignment_id, aid)
        self.assertEqual(second.assignment.assigned_to, self.admin)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        self.assertFalse(second.is_completed)

    def test_admin_post_creates_reservation_with_both_date_fields_blank(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('admin:texts_text_add_stage', args=[self.text.pk]),
            {'kind': 'first_verification', 'performer': self.other.pk,
             'started_at': '', 'ended_at': '', 'version': version_of(self.text)})
        self.assertEqual(response.status_code, 302)
        first = S.objects.get(text=self.text, stage_type='first_verification')
        self.assertIsNone(first.started_at)
        self.assertEqual(first.assignment.assigned_to, self.other)

    def test_admin_cannot_start_first_early_or_replace_genuine_dateless_completion(self):
        with self.assertRaises(ValidationError):
            self.reserve(started_at=self.today)
        first = self.false_completion()
        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
            self.reserve()
        first.refresh_from_db()
        self.assertTrue(first.is_completed)

    def test_dashboard_keeps_editor_during_pending_and_started_first_verification(self):
        first = self.reserve()
        send_to_first_verification(self.text, self.member, self.today)
        page = self.home()
        self.assertEqual(page.context['active_stage_count'], 1)
        row = page.context['active_stages'][0]
        self.assertEqual(row['pk'], first.pk)
        self.assertTrue(row['editor_waiting'])
        self.assertContains(page, 'Oczekujące')
        self.assertContains(page, 'Pierwsza weryfikacja')
        started = start_first_verification(self.text, self.other, self.today)
        self.assertEqual(self.summary()['active_stages'][0]['pk'], started.pk)
        self.assertTrue(self.summary()['active_stages'][0]['editor_waiting'])

    def test_dashboard_keeps_editor_during_editing_coordinator_control(self):
        self.close_editing()
        control = S.objects.create(text=self.text, stage_type='editing_control')
        summary = self.summary()
        self.assertEqual(summary['active_stage_count'], 1)
        self.assertEqual(summary['active_stages'][0]['pk'], control.pk)
        self.assertTrue(summary['active_stages'][0]['editor_waiting'])
        self.assertEqual(summary['reserved_assignment_count'], 0)

    def test_editor_returns_when_own_control_is_created(self):
        self.close_editing()
        S.objects.create(text=self.text, stage_type='editing_control', is_completed=True,
                         started_at=self.today, ended_at=self.today, send_to_proofreading=True)
        S.objects.create(text=self.text, stage_type='first_proofreading', is_completed=True,
                         started_at=self.today, ended_at=self.today)
        S.objects.create(text=self.text, stage_type='coordinator_control')
        self.assertEqual(self.summary()['active_stage_count'], 0)
        control = S.objects.create(text=self.text, stage_type='editor_control', assignment=self.editor)
        self.assertEqual(self.summary()['active_stage_count'], 1)
        self.assertIn(self.text.pk, [r['pk'] for r in my_texts_context(user=self.member)['texts']])

    def test_dashboard_finishes_on_approved_handoff_even_with_future_proofreading(self):
        self.close_editing()
        S.objects.create(text=self.text, stage_type='editing_control', is_completed=True,
                         started_at=self.today, ended_at=self.today, send_to_proofreading=True)
        proof = S.objects.create(text=self.text, stage_type='first_proofreading',
                                 started_at=self.today + timedelta(days=1))
        self.assertEqual(self.summary()['active_stage_count'], 0)
        S.objects.filter(pk=proof.pk).update(started_at=self.today)
        self.assertEqual(self.summary()['active_stage_count'], 0)

    def test_dashboard_waits_are_paginated_with_correct_counts_and_labels(self):
        send_to_first_verification(self.text, self.member, self.today)
        for index in range(7):
            text = Text.objects.create(title=f'Oczekujący {index}', anthology=self.book, length=100)
            assignment = A.objects.create(text=text, role='editor', assigned_to=self.member)
            S.objects.create(text=text, stage_type='editing', assignment=assignment,
                             started_at=self.today, ended_at=self.today, is_completed=True)
            S.objects.create(text=text, stage_type='first_verification')
        page = self.home()
        self.assertEqual(page.context['active_stage_count'], 8)
        self.assertEqual(len(page.context['active_stages']), 6)
        response = self.client.get(reverse('core:dashboard_tasks'), {'kind': 'active', 'page': 2})
        self.assertEqual(response.status_code, 200)
        rows = html.fromstring(response.json()['html']).xpath('//li[@class="dashboard-list-item"]')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('Oczekujące' in ''.join(row.itertext()) for row in rows))
        self.assertIsNone(response.json()['next_url'])

    def test_dashboard_does_not_expose_waits_to_unassigned_people_or_closed_texts(self):
        send_to_first_verification(self.text, self.member, self.today)
        self.assertEqual(self.summary(self.other)['active_stage_count'], 0)
        S.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.summary()['active_stage_count'], 0)
