import json
import tempfile
from io import StringIO
from pathlib import Path
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from django.http import QueryDict
from core.tests import CoreTestDataMixin, create_member
from core import test_update20 as fixtures20
from core.testing_forms import post_form
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowHandoff
from workflow.repetitions import repeat_stages, cancel_repetition
from workflow.handoffs import handoff_stage
from workflow.services import claim_stage, complete_stage, user_can_complete_stage
from core.services.texts import withdraw_text
from core.selectors.texts import text_detail_context
from core.supervision import integrity_issues
from texts.models import Text
from core.intake_forms import SingleReviewForm
from core.table_sorting import prepare_table_sort
from core.models import AnthologyCorrection
from django.test import RequestFactory


class Workflow22Tests(CoreTestDataMixin, TestCase):
    setUp = fixtures20.RepetitionTests.setUp

    def test_cancel_restores_originals_and_can_repeat_again(self):
        old=set(S.objects.current_cycle().filter(text=self.text).values_list('pk',flat=True))
        run=repeat_stages(self.text,['editing','third_proofreading'],self.superuser)
        rows=text_detail_context(user=self.superuser,text=self.text)['repeat_queue']
        self.assertEqual([row['stage_type'] for row in rows],['editing','third_proofreading'])
        cancel_repetition(self.text,self.superuser,repetition_id=run.pk)
        self.assertEqual(old,set(S.objects.current_cycle().filter(text=self.text).values_list('pk',flat=True)))
        run.refresh_from_db();self.assertIsNotNone(run.canceled_at);self.assertIsNone(run.completed_at)
        repeat_stages(self.text,['editing'],self.superuser)

    def test_cancel_denied_after_claim_and_for_non_superuser(self):
        run=repeat_stages(self.text,['editing'],self.superuser)
        with self.assertRaises(PermissionDenied):cancel_repetition(self.text,self.editor,repetition_id=run.pk)
        claim_stage(self.text,'editing',self.neweditor)
        with self.assertRaises(ValidationError):cancel_repetition(self.text,self.superuser,repetition_id=run.pk)

    def test_withdraw_closes_queue_without_fake_completion(self):
        run=repeat_stages(self.text,['editing','third_proofreading'],self.superuser)
        withdraw_text(user=self.superuser,text_id=self.text.pk)
        run.refresh_from_db();self.assertIsNotNone(run.canceled_at);self.assertIsNone(run.completed_at)
        self.assertFalse(run.stages.filter(is_completed=True).exists())
        self.assertFalse(run.stages.filter(is_current=True).exists())

    def test_handoff_preserves_identity_and_blocks_old_executor(self):
        run=repeat_stages(self.text,['editing'],self.superuser)
        stage=claim_stage(self.text,'editing',self.neweditor)
        original_start=stage.started_at;old=stage.assignment
        new=handoff_stage(self.text,self.superuser,stage_id=stage.pk,assigned_to_id=self.editor.pk,expected_assignment_id=old.pk,reason='Przekazanie zadania')
        stage.refresh_from_db();old.refresh_from_db()
        self.assertFalse(old.is_current);self.assertEqual(old.assigned_to_id,self.neweditor.pk)
        self.assertEqual(stage.assignment_id,new.pk);self.assertEqual(stage.started_at,original_start)
        self.assertFalse(user_can_complete_stage(stage,self.neweditor))
        self.assertTrue(user_can_complete_stage(stage,self.editor))
        self.assertEqual(WorkflowHandoff.objects.get().previous_assignment_id,old.pk)
        with self.assertRaises(ValidationError):handoff_stage(self.text,self.superuser,stage_id=stage.pk,assigned_to_id=self.neweditor.pk,expected_assignment_id=old.pk,reason='Stary formularz')
        complete_stage(stage,self.editor,timezone.localdate())

    def test_queue_integrity_detects_wrong_release(self):
        run=repeat_stages(self.text,['editing','third_proofreading'],self.superuser)
        run.stages.update(is_released=False)
        self.assertIn('Otwarta kolejka bez dostępnego etapu',[x['label'] for x in integrity_issues()])
        run.stages.update(is_released=True)
        self.assertIn('Przedwcześnie udostępniony etap kolejki',[x['label'] for x in integrity_issues()])


class Import22Tests(CoreTestDataMixin,TestCase):
    def test_unified_preview_commit_and_conflict_rollback(self):
        data={'schema_version':1,'source':'test22','people':[{'first_name':'Jan','last_name':'Testowy','email':'test22@example.com'}],
              'authors':[{'first_name':'Ala','last_name':'Autorka','email':'autorka22@example.com'}],
              'texts':[{'source_row':1,'title':'Nowy import','anthology':'Antologia test22','length':100,'authors':['autorka22@example.com'],
                        'stages':[{'stage_type':'editing','assigned_to':'test22@example.com'}],'next_stage':'ready'}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data.json';path.write_text(json.dumps(data))
            call_command('import_team_archive',str(path),stdout=StringIO())
            self.assertFalse(Text.objects.filter(title='Nowy import').exists())
            call_command('import_team_archive',str(path),commit=True,stdout=StringIO())
            self.assertEqual(Text.objects.filter(title='Nowy import').count(),1)
            from django.contrib.auth import get_user_model
            u=get_user_model().objects.get(email='test22@example.com');self.assertFalse(u.is_active);self.assertFalse(u.has_usable_password())
            call_command('import_team_archive',str(path),commit=True,stdout=StringIO())
            data['authors'][0]['phone_number']='123';data['texts'][0]['title']='Konflikt';path.write_text(json.dumps(data))
            with self.assertRaises(CommandError):call_command('import_team_archive',str(path),commit=True,stdout=StringIO())
            from authors.models import Author
            self.assertEqual(Author.objects.get(email='autorka22@example.com').phone_number,'')

    def test_multiple_imported_contributors_count_as_completed(self):
        from workflow.completed_import import import_completed_workflow
        from workflow.read_queries import annotate_my_work
        self.editor.email='editor22@example.com';self.editor.save(update_fields=['email'])
        user,_=create_member('other22','Redaktor');user.email='other22@example.com';user.save(update_fields=['email'])
        text=Text.objects.create(title='Dwie osoby',length=100,anthology=self.anthology)
        stages=[{'stage_type':'editing','assigned_to':u.email} for u in [self.editor,user]]
        import_completed_workflow(text_id=text.pk,stages=stages,next_stage='ready')
        self.assertEqual(S.objects.filter(text=text,stage_type='editing').count(),2)
        for u in [self.editor,user]:
            row=annotate_my_work(Text.objects.filter(pk=text.pk),u,timezone.localdate()).get()
            self.assertTrue(row.work_completed);self.assertFalse(row.work_waiting)
        self.assertFalse(import_completed_workflow(text_id=text.pk,stages=stages,next_stage='ready'))

    def test_single_form_does_not_load_all_authors(self):
        self.assertEqual(SingleReviewForm().fields['author'].queryset.count(),0)
        self.assertEqual(SingleReviewForm(initial={'author':self.author.pk}).fields['author'].queryset.count(),1)

    def test_correction_sort_mapping_available(self):
        request=RequestFactory().get('/',{'sort':'title'});request.user=self.superuser
        query,columns=prepare_table_sort(request,AnthologyCorrection.objects.all())
        self.assertEqual(columns['Tytuł opowiadania'],'title')

    def test_conflict_preserves_submitted_content_without_secrets(self):
        text=Text.objects.create(title='Konflikt formularza',length=100,anthology=self.anthology)
        self.client.force_login(self.superuser)
        response=self.client.post(reverse('core:update_coordinator_note',args=[text.pk]),{'coordinator_note':'Nie zgub tej treści <script>','_edit_version':'invalid','password':'secret-password'})
        self.assertEqual(response.status_code,409)
        self.assertContains(response,'Nie zgub tej treści &lt;script&gt;',status_code=409)
        self.assertNotContains(response,'secret-password',status_code=409)

    def test_fallback_author_search(self):
        self.client.force_login(self.superuser)
        response=self.client.get(reverse('core:review_create'),{'author_query':self.author.last_name})
        self.assertEqual(response.status_code,200)
        self.assertIn(self.author,list(response.context['fallback_authors']))
