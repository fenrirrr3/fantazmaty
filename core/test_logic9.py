from core.testing_forms import post_form
from datetime import timedelta
from unittest.mock import patch
from django.contrib import admin
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from authors.models import Author
from core.tests import CoreTestDataMixin, create_member
from core.models import WorkflowEvent
from core.services.reviews import copy_review_to_text
from core.selectors.texts import available_stages_for_user, _annotated_texts, _current_stage
from core.supervision import integrity_issues
from core.workflow_events import deliver, message
from texts.models import Text, Review
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import claim_ready_for_editing, claim_stage, complete_stage, restart_workflow_from_stage, restart_snapshot, user_can_complete_stage
from workflow.admin import WorkflowStageAdminForm


class LogicTests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.text = Text.objects.create(title='Próba workflow', anthology=self.anthology, length=100)
        self.text.authors.add(self.author)
        self.client.force_login(self.superuser)

    def stage(self, kind='ready_for_editing', **kw):
        return S.objects.create(text=self.text, stage_type=kind, workflow_cycle=self.text.current_workflow_cycle, **kw)

    def assignment(self, role='editor', user=None):
        return A.objects.create(text=self.text, workflow_cycle=self.text.current_workflow_cycle, role=role, assigned_to=user or self.editor)

    def review(self, **kw):
        defaults=dict(title='Tekst przyjęty', author=self.author, author_first_name=self.author.first_name,
            author_last_name=self.author.last_name, email=self.author.email, length=100,
            anthology=self.anthology, genre="fantasy", status=Review.Status.ACCEPTED, author_notified_at=timezone.now())
        defaults.update(kw)
        return Review.objects.create(**defaults)

    def test_restart_second_verification_is_available_and_claimable(self):
        self.stage(); self.assignment()
        verifier, _ = create_member('new_verifier', 'Weryfikator')
        restarted = restart_workflow_from_stage(self.text, 'second_verification', self.superuser, editor_id=self.editor.pk)
        rows = available_stages_for_user(user=verifier)
        self.assertIn(restarted.pk, [r['pk'] for r in rows])
        claim_stage(self.text, 'second_verification', verifier, self.today)
        restarted.refresh_from_db(); self.assertEqual(restarted.started_at,self.today)

    def test_styling_superuser_only(self):
        stage = self.stage('styling', started_at=self.today)
        self.assignment('styling', self.superuser)
        self.assertFalse(user_can_complete_stage(stage, self.coordinator))
        with self.assertRaises(PermissionDenied): complete_stage(stage, self.coordinator, self.today)
        complete_stage(stage, self.superuser, self.today)
        self.assertTrue(S.objects.filter(text=self.text,stage_type='ready').exists())

    def test_first_verifier_reservation_rejects_date(self):
        self.stage();claim_ready_for_editing(self.text,self.editor,self.today)
        verifier,_=create_member('first_verify','Weryfikator')
        with self.assertRaises(ValidationError):claim_stage(self.text,'first_verification',verifier,self.today)
        self.assertFalse(A.objects.filter(text=self.text,role='verifier_1').exists())
        claim_stage(self.text,'first_verification',verifier)
        self.assertIsNone(S.objects.get(text=self.text,stage_type='first_verification').started_at)

    def test_restart_retains_only_selected_roles(self):
        self.stage('editing',started_at=self.today)
        editor=self.assignment(); proof=self.assignment('proofreader_1')
        restart_workflow_from_stage(self.text,'first_proofreading',self.superuser,retained_ids=[editor.pk])
        self.assertEqual(list(A.objects.filter(text=self.text,workflow_cycle=2).values_list('role',flat=True)),['editor'])
        self.assertTrue(A.objects.filter(pk=proof.pk,workflow_cycle=1).exists())

    def test_restart_preview_does_not_mutate_and_rejects_stale_snapshot(self):
        self.stage(); self.assignment()
        url=reverse('core:restart_text_workflow',args=[self.text.pk])
        response=post_form(self.client, url,{'target_stage':'second_verification'})
        self.assertContains(response,'Potwierdź nowy cykl')
        self.text.refresh_from_db();self.assertEqual(self.text.current_workflow_cycle,1)
        old=restart_snapshot(self.text)
        self.assignment('proofreader_1')
        with self.assertRaises(ValidationError): restart_workflow_from_stage(self.text,'second_verification',self.superuser,editor_id=self.editor.pk,expected_snapshot=old)

    def test_restart_preview_confirmation(self):
        self.stage();a=self.assignment()
        url=reverse('core:restart_text_workflow',args=[self.text.pk])
        response=post_form(self.client, url,{'target_stage':'editor_control'})
        token=response.context['token']
        result=post_form(self.client, url,{'target_stage':'editor_control','token':token,'confirm_restart':'yes','retained_ids':[a.pk]})
        self.assertEqual(result.status_code,302)
        self.text.refresh_from_db();self.assertEqual(self.text.current_workflow_cycle,2)
        post_form(self.client, url,{'target_stage':'editor_control','token':token,'confirm_restart':'yes','retained_ids':[a.pk]})
        self.text.refresh_from_db();self.assertEqual(self.text.current_workflow_cycle,2)

    def test_coauthor_contract_must_be_confirmed_individually(self):
        self.author.has_contract=True;self.author.save()
        review=self.review()
        coauthor=Author.objects.create(first_name='Drugi',last_name='Autor',email='other@example.com')
        review.coauthors.add(coauthor)
        with self.assertRaises(ValidationError):copy_review_to_text(user=self.superuser,review_id=review.pk,contract_received=True)
        self.assertFalse(Text.objects.filter(title=review.title).exists())
        text=copy_review_to_text(user=self.superuser,review_id=review.pk,confirmed_coauthor_ids=[str(coauthor.pk)])
        coauthor.refresh_from_db();self.assertTrue(coauthor.has_contract)
        self.assertEqual(set(text.authors.values_list('pk',flat=True)),{self.author.pk,coauthor.pk})

    def test_new_author_phone_and_existing_phone_confirmation(self):
        review=self.review(author=None,email='new@example.com',phone_number='123456789')
        text=copy_review_to_text(user=self.superuser,review_id=review.pk,contract_received=True)
        self.assertEqual(text.authors.get().phone_number,'123456789')
        self.author.phone_number='111222333';self.author.has_contract=True;self.author.save()
        review=self.review(phone_number='444555666')
        copy_review_to_text(user=self.superuser,review_id=review.pk)
        self.author.refresh_from_db();self.assertEqual(self.author.phone_number,'111222333')
        other=self.review(phone_number='444555666')
        copy_review_to_text(user=self.superuser,review_id=other.pk,update_author_phone=True)
        self.author.refresh_from_db();self.assertEqual(self.author.phone_number,'444555666')

    def test_admin_correction_requires_acknowledgment_and_no_attribute_error(self):
        stage=self.stage('first_proofreading',started_at=self.today)
        data={'text':self.text.pk,'workflow_cycle':1,'stage_type':stage.stage_type,'iteration':1,'started_at':str(self.today),'ended_at':str(self.today),'is_completed':'on'}
        self.assertFalse(WorkflowStageAdminForm(data=data,instance=stage).is_valid())
        data['confirm_data_correction']='on'
        response=post_form(self.client, reverse('admin:workflow_workflowstage_change',args=[stage.pk]),data)
        self.assertEqual(response.status_code,302)
        stage.refresh_from_db();self.assertTrue(stage.is_completed)
        self.assertTrue(any(i['label']=='Brak następnego etapu po zakończeniu' for i in integrity_issues()))
        self.assertTrue(WorkflowEvent.objects.filter(text=self.text).exists())

    def test_admin_finish_creates_successor(self):
        stage=self.stage('first_proofreading',started_at=self.today)
        response=post_form(self.client, reverse('admin:workflow_workflowstage_changelist'),{'action':'finish_selected_stages','_selected_action':[stage.pk]})
        self.assertEqual(response.status_code,302)
        self.assertTrue(S.objects.filter(text=self.text,stage_type='second_proofreading').exists())

    def test_inconsistent_completion_and_duplicate_open_stage(self):
        stage=self.stage('styling')
        with self.assertRaises(ValidationError):S(text=self.text,stage_type='styling',iteration=2).full_clean()
        stage.started_at=stage.ended_at=self.today
        with self.assertRaises(ValidationError):stage.full_clean()

    def test_state_ignores_pk_and_null_dates_for_semantic_order(self):
        control=self.stage('editor_control')
        earlier=self.stage('first_proofreading')
        self.assertEqual(_current_stage([earlier,control]),control)
        annotated=_annotated_texts().get(pk=self.text.pk)
        self.assertEqual(annotated.current_stage_type,'editor_control')

    @override_settings(DISCORD_WEBHOOKS={'Testowy':'https://discord.com/api/webhooks/123/token'})
    @patch('core.discord_webhook.send_message',return_value='123')
    def test_discord_after_commit_once_and_content(self, send):
        self.stage()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            claim_ready_for_editing(self.text,self.editor,self.today)
            send.assert_not_called()
        self.assertEqual(len(callbacks),1)
        callbacks[0]();event=WorkflowEvent.objects.get(text=self.text)
        self.assertEqual(event.status,'sent');self.assertEqual(send.call_count,1)
        payload=send.call_args.args[1]
        for item in (self.text.title,str(self.author),'Do redakcji','Redakcja','Rozpoczęcie etapu'):
            self.assertIn(item,payload)
        self.assertNotIn(self.author.email,payload)
        deliver(event.pk);self.assertEqual(send.call_count,1)

    @patch('core.discord_webhook.send_message')
    def test_rollback_does_not_send_or_record(self, send):
        self.stage()
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    claim_ready_for_editing(self.text,self.editor,self.today)
                    raise RuntimeError('rollback')
        self.assertFalse(WorkflowEvent.objects.exists());send.assert_not_called()
        self.assertTrue(S.objects.filter(text=self.text,stage_type='ready_for_editing').exists())

    @override_settings(DISCORD_WEBHOOKS={'Testowy':'https://discord.com/api/webhooks/123/token'})
    @patch('core.discord_webhook.send_message',side_effect=RuntimeError('network'))
    def test_delivery_failure_keeps_workflow_saved(self, send):
        self.stage()
        with self.captureOnCommitCallbacks(execute=True):claim_ready_for_editing(self.text,self.editor,self.today)
        self.assertTrue(S.objects.filter(text=self.text,stage_type='editing',started_at=self.today).exists())
        self.assertEqual(WorkflowEvent.objects.get().status,'unknown')

    def test_copy_form_shows_coauthors_and_phone_choice(self):
        self.author.has_contract=True;self.author.phone_number='111222333';self.author.save()
        review=self.review(phone_number='444555666')
        coauthor=Author.objects.create(first_name='Współautor',last_name='Testowy',email='coauthor@example.com')
        review.coauthors.add(coauthor)
        response=self.client.get(reverse('core:assigned_review_detail',args=[review.pk]))
        self.assertContains(response, 'name="coauthor_contract_received"')
        self.assertContains(response, 'name="update_author_phone"')
        self.assertContains(response, '111222333')

    def test_restart_editing_with_retained_editor_creates_verification_queue(self):
        from core.services.texts import start_assigned_stage
        self.stage(); a=self.assignment()
        stage=restart_workflow_from_stage(self.text,'editing',self.superuser,retained_ids=[a.pk])
        start_assigned_stage(user=self.editor,stage_id=stage.pk,started_at=self.today)
        self.assertTrue(S.objects.filter(text=self.text,workflow_cycle=2,stage_type='first_verification',started_at__isnull=True).exists())

    def test_restart_author_phase_can_continue_without_inventing_history(self):
        from core.services.texts import start_assigned_stage
        from workflow.services import resume_editing, send_to_second_verification
        self.stage();a=self.assignment()
        stage=restart_workflow_from_stage(self.text,'author_editing',self.superuser,retained_ids=[a.pk])
        self.assertIsNone(stage.started_at)
        start_assigned_stage(user=self.editor,stage_id=stage.pk,started_at=self.today)
        resume_editing(self.text,self.editor,self.today)
        send_to_second_verification(self.text,self.editor,self.today)
        self.assertTrue(S.objects.filter(text=self.text,workflow_cycle=2,stage_type='second_verification').exists())
        self.assertFalse(S.objects.filter(text=self.text,workflow_cycle=2,stage_type='first_verification').exists())
