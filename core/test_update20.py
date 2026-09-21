from core.edit_versions import version_of
from datetime import timedelta
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin, create_member
from core.testing_forms import post_form
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import STAGE_ROLES, claim_stage, complete_stage, user_can_complete_stage
from workflow.repetitions import repeat_stages
from core.services.texts import start_assigned_stage, change_scheduled_stage
from core.selectors.texts import text_detail_context, available_stages_for_user, my_texts_context
from core.views.people import _profile_assignments
from core.services.vacations import create_vacation
from texts.models import Text

class RepetitionTests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.today=timezone.localdate()
        self.proofreader,self.proofperson=create_member('proof20','Korektor')
        self.verifier,self.verifierperson=create_member('verif20','Weryfikator')
        self.verifier2,_=create_member('verif202','Weryfikator')
        self.neweditor,_=create_member('edit20','Redaktor')
        self.text=Text.objects.create(title='Powtórzenie', anthology=self.anthology, length=1000)
        self.text.authors.add(self.author)
        self.originals={}
        for kind in ('editing','first_verification','second_verification','editor_control','third_proofreading','styling'):
            role=STAGE_ROLES[kind]
            user=self.editor if role=='editor' else self.verifier if role=='verifier_1' else self.verifier2 if role=='verifier_2' else self.superuser
            a,_=A.objects.get_or_create(text=self.text,role=role,defaults={'assigned_to':user})
            self.originals[kind]=S.objects.create(text=self.text,stage_type=kind,started_at=self.today,ended_at=self.today,is_completed=True,assignment=a)
        S.objects.create(text=self.text,stage_type='ready',started_at=self.today)

    def test_selective_preserves_history_and_queues(self):
        original=list(S.objects.filter(text=self.text).values('pk','stage_type','started_at','ended_at','assignment_id'))
        run=repeat_stages(self.text,['third_proofreading','editing'],self.superuser)
        steps=list(run.stages.order_by('queue_position'))
        self.assertEqual([s.stage_type for s in steps],['editing','third_proofreading'])
        self.assertEqual([s.is_released for s in steps],[True,False])
        self.assertTrue(all(s.started_at is None and s.assignment.assigned_to_id is None and s.execution_number==2 for s in steps))
        self.assertEqual(original,list(S.objects.filter(pk__in=[s['pk'] for s in original]).values('pk','stage_type','started_at','ended_at','assignment_id')))
        self.originals['first_verification'].refresh_from_db();self.assertTrue(self.originals['first_verification'].is_current)
        self.originals['editing'].refresh_from_db();self.assertFalse(self.originals['editing'].is_current)
        with self.assertRaises(ValidationError):claim_stage(self.text,'third_proofreading',self.proofreader)
        self.assertNotIn(steps[1].pk,[r['pk'] for r in available_stages_for_user(user=self.proofreader)])
        editing=claim_stage(self.text,'editing',self.neweditor)
        self.assertFalse(user_can_complete_stage(editing,self.editor))
        complete_stage(editing,self.neweditor,self.today)
        proof=claim_stage(self.text,'third_proofreading',self.proofreader)
        complete_stage(proof,self.proofreader,self.today)
        run.refresh_from_db();self.assertIsNotNone(run.completed_at)
        self.assertEqual(S.objects.current_cycle().filter(text=self.text,stage_type='ready').count(),1)
        self.assertEqual(S.objects.filter(text=self.text,stage_type='fourth_proofreading').count(),0)
        second=repeat_stages(self.text,['third_proofreading'],self.superuser)
        self.assertEqual(second.stages.get().execution_number,3)

    def test_editor_returns_only_for_selected_verifications(self):
        run=repeat_stages(self.text,['editing','second_verification'],self.superuser)
        self.assertEqual(list(run.stages.order_by('queue_position').values_list('stage_type',flat=True)),['editing','second_verification','editing'])
        stage=claim_stage(self.text,'editing',self.neweditor);complete_stage(stage,self.neweditor,self.today)
        verification=claim_stage(self.text,'second_verification',self.verifier2);complete_stage(verification,self.verifier2,self.today)
        last=run.stages.get(stage_type='editing',is_completed=False)
        start_assigned_stage(user=self.neweditor,stage_id=last.pk,started_at=self.today)
        last.refresh_from_db();complete_stage(last,self.neweditor,self.today)
        self.assertEqual(run.stages.filter(stage_type='first_verification').count(),0)
        self.assertEqual(set(run.stages.filter(stage_type='editing').values_list('execution_number',flat=True)),{2})

    def test_rejects_working_ready_anthology_and_double_submission(self):
        snapshot=version_of(self.text)
        repeat_stages(self.text,['editing'],self.superuser,expected_version=snapshot)
        with self.assertRaises(ValidationError):repeat_stages(self.text,['editing'],self.superuser,expected_version=snapshot)
        self.anthology.status='ready';self.anthology.save()
        with self.assertRaises(ValidationError):repeat_stages(self.text,['editing'],self.superuser)
        with self.assertRaises((PermissionDenied,ValidationError)):repeat_stages(self.text,['editing'],self.editor)

    def test_history_contact_and_readonly_new_execution(self):
        run=repeat_stages(self.text,['editing'],self.superuser)
        self.client.force_login(self.editor)
        response=self.client.get(reverse('core:assigned_text_detail',args=[self.text.pk]))
        self.assertEqual(response.status_code,200)
        self.assertIn(self.author.email,response.context['author_emails'])
        self.assertFalse(response.context['can_add_note'])
        self.client.force_login(self.coordinator)
        response=self.client.get(reverse('core:assigned_text_detail',args=[self.text.pk]))
        self.assertIn(self.author.email,response.context['author_emails'])
        self.assertContains(response,'Kolejka powtórzeń')

    def test_counts_match_profile_and_my_texts(self):
        repeat_stages(self.text,['editing','third_proofreading'],self.superuser)
        stage=claim_stage(self.text,'editing',self.neweditor)
        complete_stage(stage,self.neweditor,self.today)
        from workflow.read_queries import filter_my_texts
        self.assertTrue(filter_my_texts(Text.objects.filter(pk=self.text.pk),self.neweditor,'completed',self.today).exists())
        context=my_texts_context(user=self.neweditor,selected_view='completed')
        rows=list(context['texts']);self.assertTrue(rows[0]['has_completed_work'])
        _,summary=_profile_assignments(self.neweditor.person_profile,include_authors=False)
        self.assertEqual(summary['completed'],1)

    def test_leave_and_future_queue_blocked(self):
        run=repeat_stages(self.text,['editing','third_proofreading'],self.superuser)
        create_vacation(user=self.coordinator,person_id=self.editor_person.pk,start_date=self.today,until_revoked=True)
        with self.assertRaises(ValidationError):claim_stage(self.text,'editing',self.editor)
        stage=run.stages.get(stage_type='third_proofreading')
        a=stage.assignment;a.assigned_to=self.proofreader;a.save()
        with self.assertRaises(ValidationError):start_assigned_stage(user=self.proofreader,stage_id=stage.pk,started_at=self.today)

    def test_preview_confirm_http(self):
        self.client.force_login(self.superuser)
        url=reverse('core:restart_text_workflow',args=[self.text.pk])
        response=post_form(self.client,url,{'stages':['editing','third_proofreading']})
        self.assertEqual(response.status_code,200)
        response=post_form(self.client,url,{'stages':['editing','third_proofreading'],'token':response.context['token'],'confirm_restart':'yes'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.text.repetitions.count(),1)

    def test_first_verification_http_uses_repeat_queue(self):
        run=repeat_stages(self.text,['first_verification'],self.superuser)
        stage=run.stages.get()
        self.client.force_login(self.verifier)
        response=post_form(self.client,reverse('core:take_workflow_stage',args=[stage.pk]), {'started_at':self.today.isoformat()})
        self.assertEqual(response.status_code,302)
        stage.refresh_from_db();self.assertEqual(stage.started_at,self.today)
        self.assertNotIn('Zarezerwowano pierwszą',str(list(response.wsgi_request._messages)))
        complete_stage(stage,self.verifier,self.today)
        self.assertTrue(S.objects.current_cycle().filter(text=self.text,stage_type='ready').exists())

    def test_cancel_repeat_reservation_preserves_stage_type(self):
        run=repeat_stages(self.text,['editing'],self.superuser)
        stage=claim_stage(self.text,'editing',self.neweditor,self.today+timedelta(days=2))
        change_scheduled_stage(user=self.neweditor,stage_id=stage.pk,cancel=True)
        stage.refresh_from_db();self.assertEqual(stage.stage_type,'editing');self.assertIsNone(stage.started_at)
        self.assertIsNone(A.objects.get(pk=stage.assignment_id).assigned_to_id)
        stage=claim_stage(self.text,'editing',self.editor)
        self.assertEqual(stage.repetition_id,run.pk)

    def test_credits_keep_both_actual_editors_and_integrity_accepts_queue(self):
        from core.supervision import anthology_credits, integrity_issues
        repeat_stages(self.text,['editing','second_verification'],self.superuser)
        labels=[row['label'] for row in integrity_issues()]
        self.assertNotIn('Kilka otwartych etapów tego samego rodzaju', labels)
        self.assertNotIn('Brak następnego etapu po zakończeniu', labels)
        stage=claim_stage(self.text,'editing',self.neweditor);complete_stage(stage,self.neweditor,self.today)
        credits=anthology_credits(self.anthology)
        editor_names={row['name'] for row in credits if row['role']=='Redaktor'}
        self.assertIn(str(self.editor_person),editor_names)
        self.assertIn(str(self.neweditor.person_profile),editor_names)

    def test_coordinator_no_staff_and_group_does_not_override_profile(self):
        from django.contrib.auth.models import Group
        from core.permissions import is_coordinator
        from people.models import Role
        self.editor_person.roles.add(Role.objects.get_or_create(name='Koordynator redakcji')[0])
        self.editor.refresh_from_db();self.assertFalse(self.editor.is_staff)
        self.assertTrue(is_coordinator(self.editor))
        group=Group.objects.create(name='Koordynator testowy')
        self.editor.groups.add(group);self.editor.groups.remove(group)
        self.assertTrue(is_coordinator(self.editor))

    def test_sort_semantic_stage_order_and_public_team_email(self):
        from django.test import RequestFactory
        from core.pagination import paginate_items
        from people.models import Person
        request=RequestFactory().get('/',{'sort':'stage'});request.user=self.editor
        page=paginate_items(request,S.objects.current_cycle().filter(text=self.text))
        values=[s.stage_type for s in page]
        self.assertLess(values.index('editing'),values.index('first_verification'))
        request=RequestFactory().get('/',{'sort':'email'});request.user=self.editor
        page=paginate_items(request,Person.objects.all())
        self.assertIn('email',page.sort_columns.values())

    def test_repeat_event_is_commit_bound_and_has_execution(self):
        from unittest.mock import patch
        from core.models import WorkflowEvent
        with patch('core.workflow_events.notify_after_commit') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                repeat_stages(self.text,['editing'],self.superuser)
                self.assertFalse(notify.called)
            self.assertTrue(notify.called)
        event=WorkflowEvent.objects.latest('pk')
        self.assertIn('wykonanie 2',event.details)
        self.assertIn('Gotowe',event.previous_status)


from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

class WorkflowUpgradeTests(TransactionTestCase):
    def test_existing_cycle_dates_and_owner_are_bound_without_rewriting(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        old=[node for node in latest if node[0] != 'workflow'] + [('workflow','0002_mysql_verifier_uniqueness')]
        try:
            executor.migrate(old)
            apps=executor.loader.project_state(old).apps
            User=apps.get_model('auth','User'); T=apps.get_model('texts','Text'); Anthology=apps.get_model('texts','Anthology')
            OldA=apps.get_model('workflow','WorkflowRoleAssignment'); OldS=apps.get_model('workflow','WorkflowStage')
            user=User.objects.create(username='migration20')
            anthology=Anthology.objects.create(title='Migracja')
            text=T.objects.create(title='Historia',anthology=anthology,length=10,current_workflow_cycle=2)
            assignment=OldA.objects.create(text_id=text.pk,workflow_cycle=1,role='editor',assigned_to_id=user.pk)
            stage=OldS.objects.create(text_id=text.pk,workflow_cycle=1,stage_type='editing',started_at='2020-01-01',ended_at='2020-01-02',is_completed=True)
            executor=MigrationExecutor(connection);executor.migrate(latest)
            new=S.objects.get(pk=stage.pk)
            self.assertEqual(new.assignment_id,assignment.pk)
            self.assertEqual(new.started_at.isoformat(),'2020-01-01')
            self.assertEqual(new.ended_at.isoformat(),'2020-01-02')
            self.assertEqual(new.workflow_cycle,1)
            self.assertEqual(Text.objects.get(pk=text.pk).current_workflow_cycle,2)
        finally:
            MigrationExecutor(connection).migrate(latest)
