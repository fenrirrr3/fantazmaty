from datetime import timedelta
from django.test import TestCase, RequestFactory
from django.core.exceptions import ValidationError, PermissionDenied
from django.urls import reverse
from django.utils import timezone
from django.forms.models import inlineformset_factory
from core.tests import CoreTestDataMixin
from core.workflow_tokens import make_token
from core.forms import CompleteStageForm
from texts.models import Text
from texts.admin import WorkflowAssignmentFormSet
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import restart_workflow_from_stage, complete_stage
from workflow.admin import WorkflowRoleAssignmentAdminForm, WorkflowRoleAssignmentAdmin
from django.contrib import admin


class Logic10Tests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.today=timezone.localdate()
        self.text=Text.objects.create(title='Dalsze testy',length=100)
        self.client.force_login(self.superuser)

    def test_restart_uses_maximum_of_stages_and_assignments(self):
        S.objects.create(text=self.text,workflow_cycle=2,stage_type='ready_for_editing')
        A.objects.create(text=self.text,workflow_cycle=4,role='editor',assigned_to=self.editor)
        stage=restart_workflow_from_stage(self.text,'ready_for_editing',self.superuser)
        self.assertEqual(stage.workflow_cycle,5)
        self.assertTrue(S.objects.filter(text=self.text,workflow_cycle=2).exists())

    def test_missing_or_inactive_editor_blocks_completion_atomically(self):
        stage=S.objects.create(text=self.text,stage_type='coordinator_control',started_at=self.today)
        for state in ('missing','inactive'):
            if state=='inactive':
                A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
                self.editor.is_active=False;self.editor.save()
            with self.assertRaises(ValidationError):complete_stage(stage,self.superuser,self.today)
            stage.refresh_from_db();self.assertFalse(stage.is_completed)
            self.assertFalse(S.objects.filter(text=self.text,stage_type='editor_control').exists())
        self.editor.is_active=True;self.editor.save()
        complete_stage(stage,self.superuser,self.today)
        self.assertEqual(S.objects.get(text=self.text,stage_type='editor_control').started_at,self.today)

    def test_future_completion_rejected_in_form_service_and_admin_model(self):
        stage=S.objects.create(text=self.text,stage_type='styling',started_at=self.today)
        future=self.today+timedelta(days=2)
        self.assertFalse(CompleteStageForm(data={'ended_at':future},stage=stage).is_valid())
        with self.assertRaises(ValidationError):complete_stage(stage,self.superuser,future)
        stage.ended_at=future;stage.is_completed=True
        with self.assertRaises(ValidationError):stage.full_clean()
        self.assertFalse(S.objects.filter(text=self.text,stage_type='ready').exists())

    def test_inline_add_uses_current_cycle(self):
        self.text.current_workflow_cycle=3;self.text.save()
        Factory=inlineformset_factory(Text,A,formset=WorkflowAssignmentFormSet,fields=('role','assigned_to','notes'),extra=1)
        fs=Factory(instance=self.text,prefix='a',data={'a-TOTAL_FORMS':'1','a-INITIAL_FORMS':'0','a-MAX_NUM_FORMS':'1000','a-0-role':'editor','a-0-assigned_to':self.editor.pk,'a-0-notes':''})
        self.assertTrue(fs.is_valid(),fs.errors)
        fs.save()
        self.assertEqual(A.objects.get(text=self.text).workflow_cycle,3)

    def test_admin_cannot_rewrite_performer_but_notes_remain_editable(self):
        S.objects.create(text=self.text,stage_type='editing',started_at=self.today)
        a=A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        data={'text':self.text.pk,'workflow_cycle':1,'role':'editor','assigned_to':self.superuser.pk,'notes':'korekta'}
        self.assertFalse(WorkflowRoleAssignmentAdminForm(data=data,instance=a).is_valid())
        a.refresh_from_db();data['assigned_to']=self.editor.pk
        form=WorkflowRoleAssignmentAdminForm(data=data,instance=a)
        self.assertTrue(form.is_valid(),form.errors)
        form.save();a.refresh_from_db();self.assertEqual(a.notes,'korekta')
        request=RequestFactory().post('/');request.user=self.superuser
        self.assertFalse(WorkflowRoleAssignmentAdmin(A,admin.site).has_delete_permission(request,a))

    def test_inline_cannot_delete_recorded_assignment(self):
        a=A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        S.objects.create(text=self.text,stage_type='editing',started_at=self.today)
        Factory=inlineformset_factory(Text,A,formset=WorkflowAssignmentFormSet,fields=('role','assigned_to','notes'),extra=0)
        fs=Factory(instance=self.text,prefix='a',data={'a-TOTAL_FORMS':'1','a-INITIAL_FORMS':'1','a-0-id':a.pk,'a-0-role':'editor','a-0-assigned_to':self.editor.pk,'a-0-DELETE':'on'})
        self.assertFalse(fs.is_valid())

    def test_stale_tab_cannot_withdraw_new_cycle(self):
        S.objects.create(text=self.text,stage_type='ready_for_editing')
        token=make_token(self.text,self.superuser)
        restart_workflow_from_stage(self.text,'ready_for_editing',self.superuser)
        url=reverse('core:withdraw_text',args=[self.text.pk])
        self.client.post(url,{'workflow_token':token})
        self.assertFalse(S.objects.filter(text=self.text,stage_type='withdrawn').exists())
        self.client.post(url,{'workflow_token':make_token(self.text,self.superuser)})
        self.assertTrue(S.objects.filter(text=self.text,stage_type='withdrawn').exists())

    def test_text_transition_rejects_changed_assignments_and_missing_token(self):
        S.objects.create(text=self.text,stage_type='editing',started_at=self.today)
        S.objects.create(text=self.text,stage_type='first_verification')
        A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        token=make_token(self.text,self.superuser)
        A.objects.create(text=self.text,role='verifier_1',assigned_to=self.superuser)
        url=reverse('core:send_to_first_verification_stage',args=[self.text.pk])
        for data in ({},{'workflow_token':token}):
            self.client.post(url,data)
            self.assertFalse(S.objects.get(text=self.text,stage_type='editing').is_completed)
        self.client.post(url,{'workflow_token':make_token(self.text,self.superuser)})
        self.assertTrue(S.objects.get(text=self.text,stage_type='editing').is_completed)

    def test_inline_allows_new_unsaved_text(self):
        text=Text(title='Nowy',length=100)
        Factory=inlineformset_factory(Text,A,formset=WorkflowAssignmentFormSet,fields=('role','assigned_to','notes'),extra=0)
        fs=Factory(instance=text,prefix='a',data={'a-TOTAL_FORMS':'0','a-INITIAL_FORMS':'0'})
        self.assertTrue(fs.is_valid(),fs.errors)
