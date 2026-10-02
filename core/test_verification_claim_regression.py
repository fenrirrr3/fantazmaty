from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from people.models import Person, Role
from core.selectors.texts import _annotated_texts, text_detail_context
from core.services.texts import start_assigned_stage
from texts.models import Anthology, Text
from workflow.availability import claim_access, claim_reason
from workflow.import_context import importing_completed
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.read_queries import available_stages
from workflow.services import claim_ready_for_editing, claim_stage, create_pending_stage, resume_editing
from workflow.repetitions import repeat_stages
from workflow.state import current_stage


class VerificationClaimRegressionTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.today = timezone.localdate()
        self.user = get_user_model().objects.create_user('member', 'member@example.com', 'test')
        self.other = get_user_model().objects.create_user('other', 'other@example.com', 'test')
        person = Person.objects.create(user=self.user, email=self.user.email)
        for role in ('Redaktor', 'Weryfikator', 'Korektor'):
            person.roles.add(Role.objects.get_or_create(name=role)[0])
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'test')
        self.text = Text.objects.create(title='Stary tekst', length=100,
                                        anthology=Anthology.objects.create(title='Antologia'))
        self.client.force_login(self.user)

    def imported(self, kind, *, role=None, current=True):
        assignment = (A.objects.create(text=self.text, role=role, assigned_to=self.other,
                                        is_current=False) if role else None)
        token = importing_completed.set(True)
        try:
            return S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                is_completed=True, imported_completed=True, is_current=current,
                started_at=None, ended_at=None)
        finally:
            importing_completed.reset(token)

    def obsolete_first(self):
        completed = self.imported('first_verification', role='verifier_1')
        self.imported('editing')
        pending = S.objects.create(text=self.text, stage_type='first_verification', iteration=2)
        return completed, pending

    def test_completed_first_hides_legacy_pending_row_and_blocks_claim_policy(self):
        completed, pending = self.obsolete_first()
        self.assertFalse(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        reason = claim_reason(pending, self.user,
            list(S.objects.current_cycle().filter(text=self.text)),
            list(A.objects.current_cycle().filter(text=self.text)))
        self.assertIn('już zakończona', reason)
        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
            claim_stage(self.text, 'first_verification', self.user)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertIsNone(completed.started_at)
        self.assertIsNone(completed.ended_at)

    def test_click_on_obsolete_first_returns_message_without_500_or_new_assignment(self):
        completed, pending = self.obsolete_first()
        response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]))
        self.assertRedirects(response, reverse('core:available_texts'), fetch_redirect_response=False)
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertTrue(any('już zakończona' in message for message in messages))
        pending.refresh_from_db()
        self.assertIsNone(pending.assignment_id)
        self.assertIsNone(pending.started_at)
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        completed.refresh_from_db()
        self.assertEqual(completed.assignment.assigned_to_id, self.other.pk)

    def test_automatic_editing_claim_keeps_completed_first_without_creating_another(self):
        completed = self.imported('first_verification', role='verifier_1')
        S.objects.create(text=self.text, stage_type='ready_for_editing')
        editing = claim_ready_for_editing(self.text, self.user)
        self.assertEqual(editing.stage_type, 'editing')
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertIsNone(completed.started_at)

    def test_trusted_pending_creation_cannot_repeat_completed_verification(self):
        self.imported('first_verification', role='verifier_1')
        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
            create_pending_stage(self.text, 'first_verification')
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)

    def test_claim_with_historical_assignment_uses_next_execution_number(self):
        old = self.imported('first_proofreading', role='proofreader_1', current=False)
        pending = S.objects.create(text=self.text, stage_type='first_proofreading', iteration=2)
        response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]),
                                     {'started_at': self.today.isoformat()})
        self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]),
                             fetch_redirect_response=False)
        pending.refresh_from_db()
        self.assertEqual(pending.started_at, self.today)
        self.assertEqual(pending.assignment.assigned_to_id, self.user.pk)
        self.assertEqual(pending.assignment.execution_number, 2)
        old.refresh_from_db()
        self.assertEqual(old.assignment.execution_number, 1)
        self.assertEqual(old.assignment.assigned_to_id, self.other.pk)

    def test_old_empty_first_does_not_block_continued_editing_or_misreport_status(self):
        completed, obsolete = self.obsolete_first()
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        editing = S.objects.create(text=self.text, stage_type='editing', iteration=2, assignment=editor)
        detail = text_detail_context(user=self.user, text=self.text)
        self.assertFalse(detail['can_start_first_verification'])
        self.assertIsNone(detail['pending_first_verification'])
        self.assertTrue(detail['can_resume_editing'])
        self.assertEqual(current_stage(self.text.workflow_stages.all()).pk, editing.pk)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'editing')
        start_assigned_stage(user=self.user, stage_id=editing.pk, started_at=self.today)
        editing.refresh_from_db()
        self.assertEqual(editing.started_at, self.today)
        detail = text_detail_context(user=self.user, text=self.text)
        self.assertFalse(detail['can_send_to_first_verification'])
        self.assertTrue(detail['can_send_to_second_verification'])
        self.assertTrue(detail['can_send_to_author'])
        obsolete.refresh_from_db()
        self.assertIsNone(obsolete.started_at)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)

    def test_resume_ignores_empty_duplicate_but_keeps_actual_verification_gate(self):
        self.obsolete_first()
        A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        editing = resume_editing(self.text, self.user)
        self.assertEqual(editing.started_at, self.today)

    def test_completed_second_is_not_available_again(self):
        self.imported('first_verification', role='verifier_1')
        self.imported('second_verification', role='verifier_2')
        self.imported('editing')
        pending = S.objects.create(text=self.text, stage_type='second_verification', iteration=2)
        self.assertFalse(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        with self.assertRaisesMessage(ValidationError, 'Druga weryfikacja jest już zakończona'):
            claim_stage(self.text, 'second_verification', self.user)

    def test_explicit_repetition_still_allows_another_first_verification(self):
        self.imported('first_verification', role='verifier_1')
        S.objects.create(text=self.text, stage_type='ready')
        run = repeat_stages(self.text, ['first_verification'], self.admin)
        repeated = run.stages.get()
        self.assertTrue(available_stages(self.user, claim_access(self.user)).filter(pk=repeated.pk).exists())
        response = self.client.post(reverse('core:take_workflow_stage', args=[repeated.pk]),
                                     {'started_at': self.today.isoformat()})
        self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]),
                             fetch_redirect_response=False)
        repeated.refresh_from_db()
        self.assertEqual(repeated.started_at, self.today)
        self.assertEqual(repeated.assignment.assigned_to_id, self.user.pk)

    def test_previous_cycle_completion_does_not_block_new_first_verification(self):
        completed = self.imported('first_verification', role='verifier_1')
        self.text.current_workflow_cycle = 2
        self.text.save()
        pending = S.objects.create(text=self.text, workflow_cycle=2, stage_type='first_verification')
        self.assertTrue(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        claim_stage(self.text, 'first_verification', self.user, stage_id=pending.pk)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertIsNone(completed.started_at)

    def test_endpoint_claims_clicked_execution_instead_of_another_pending_row(self):
        clicked = S.objects.create(text=self.text, stage_type='first_proofreading')
        other = S.objects.create(text=self.text, stage_type='first_proofreading', iteration=2)
        response = self.client.post(reverse('core:take_workflow_stage', args=[clicked.pk]),
                                     {'started_at': self.today.isoformat()})
        self.assertEqual(response.status_code, 302)
        clicked.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(clicked.started_at, self.today)
        self.assertIsNone(other.started_at)

    def test_unexpected_assignment_conflict_returns_message_and_keeps_database_unchanged(self):
        pending = S.objects.create(text=self.text, stage_type='first_proofreading')
        with patch('core.views.workflow.claim_stage', side_effect=IntegrityError('assignment conflict')):
            with self.assertLogs('core.views.workflow', level='ERROR'):
                response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]),
                                             {'started_at': self.today.isoformat()})
        self.assertRedirects(response, reverse('core:available_texts'), fetch_redirect_response=False)
        self.assertTrue(any('Nie udało się przejąć etapu' in str(message)
                            for message in response.wsgi_request._messages))
        pending.refresh_from_db()
        self.assertIsNone(pending.started_at)
        self.assertFalse(A.objects.filter(text=self.text).exists())
