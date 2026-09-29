from django.test import TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError,PermissionDenied
from django.urls import reverse
from django.utils import timezone
from people.models import Person,Role
from texts.models import Text,Anthology
from workflow.models import WorkflowStage as S,WorkflowRoleAssignment as A
from workflow.services import claim_stage,skip_fourth_proofreading
from workflow.availability import claim_access
from workflow.read_queries import available_stages


class ProofreadingPolicyTests(TestCase):
    def setUp(self):
        self.admin=get_user_model().objects.create_superuser('admin','admin@example.com','test')
        self.user=get_user_model().objects.create_user('proof')
        person=Person.objects.create(user=self.user,first_name='A',last_name='B',email='proof@example.com',is_active=True)
        role,_=Role.objects.get_or_create(name='Korektor');person.roles.add(role)
        group,_=Group.objects.get_or_create(name='Korektor');self.user.groups.add(group)
        self.text=Text.objects.create(title='Test',length=200)

    def test_first_performer_cannot_see_or_take_second_and_third(self):
        first=A.objects.create(text=self.text,role=A.Role.PROOFREADER_1,assigned_to=self.user)
        S.objects.create(text=self.text,stage_type=S.StageType.FIRST_PROOFREADING,assignment=first,started_at=timezone.localdate(),ended_at=timezone.localdate(),is_completed=True,is_current=False)
        for kind in [S.StageType.SECOND_PROOFREADING,S.StageType.THIRD_PROOFREADING]:
            stage=S.objects.create(text=self.text,stage_type=kind)
            self.assertNotIn(stage.pk,available_stages(self.user,claim_access(self.user)).values_list('pk',flat=True))
            with self.assertRaises(ValidationError):claim_stage(self.text,kind,self.user)
            self.assertFalse(A.objects.filter(text=self.text,role__in=[A.Role.PROOFREADER_2,A.Role.PROOFREADER_3]).exists())
            stage.delete()

    def test_proofreading_coordinator_can_take_second(self):
        role,_=Role.objects.get_or_create(name="Koordynator korekty")
        self.user.person_profile.roles.add(role)
        stage=S.objects.create(text=self.text,stage_type=S.StageType.SECOND_PROOFREADING)
        self.assertIn(stage.pk,available_stages(self.user,claim_access(self.user)).values_list('pk',flat=True))
        claim_stage(self.text,stage.stage_type,self.user)
        stage.refresh_from_db();self.assertEqual(stage.assignment.assigned_to_id,self.user.pk)

    def test_skip_leaves_empty_completed_record_and_releases_styling(self):
        stage=S.objects.create(text=self.text,stage_type=S.StageType.FOURTH_PROOFREADING)
        skip_fourth_proofreading(stage,self.admin)
        stage.refresh_from_db();self.assertTrue(stage.is_skipped);self.assertTrue(stage.is_completed)
        self.assertIsNone(stage.assignment_id);self.assertIsNone(stage.started_at);self.assertIsNone(stage.ended_at)
        stage.full_clean()
        self.assertTrue(S.objects.filter(text=self.text,stage_type=S.StageType.STYLING,is_completed=False).exists())
        with self.assertRaises(ValidationError):skip_fourth_proofreading(stage,self.admin)

    def test_skip_rejects_non_superuser_wrong_stage_and_assigned_work(self):
        stage=S.objects.create(text=self.text,stage_type=S.StageType.FOURTH_PROOFREADING)
        with self.assertRaises(PermissionDenied):skip_fourth_proofreading(stage,self.user)
        assignment=A.objects.create(text=self.text,role=A.Role.PROOFREADER_4,assigned_to=self.admin)
        stage.assignment=assignment;stage.save()
        with self.assertRaises(ValidationError):skip_fourth_proofreading(stage,self.admin)
        stage.refresh_from_db();self.assertFalse(stage.is_completed)

    def test_skip_button_and_endpoint_permissions(self):
        stage=S.objects.create(text=self.text,stage_type=S.StageType.FOURTH_PROOFREADING)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('core:assigned_text_detail',args=[self.text.pk])),'Pomiń etap')
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse('core:assigned_text_detail',args=[self.text.pk])),'Pomiń etap')
        self.assertEqual(self.client.post(reverse('core:skip_workflow_stage',args=[stage.pk])).status_code,403)

    def test_skip_repeated_fourth_with_empty_assignment(self):
        from workflow.repetitions import repeat_stages
        S.objects.create(text=self.text,stage_type=S.StageType.FOURTH_PROOFREADING,is_completed=True,is_skipped=True)
        ready=S.objects.create(text=self.text,stage_type=S.StageType.READY)
        run=repeat_stages(self.text,[S.StageType.FOURTH_PROOFREADING],self.admin)
        stage=run.stages.get()
        self.assertIsNotNone(stage.assignment_id)
        self.assertIsNone(stage.assignment.assigned_to_id)
        skip_fourth_proofreading(stage,self.admin)
        run.refresh_from_db();stage.refresh_from_db()
        self.assertIsNotNone(run.completed_at)
        self.assertTrue(stage.is_skipped)
        self.assertIsNone(stage.assignment_id)

    def test_ready_anthology_cannot_be_changed(self):
        anthology=Anthology.objects.create(title='Wydana',status='ready')
        Text.objects.filter(pk=self.text.pk).update(anthology=anthology)
        self.text.refresh_from_db()
        stage=S.objects.create(text=self.text,stage_type=S.StageType.FOURTH_PROOFREADING)
        with self.assertRaises(ValidationError):skip_fourth_proofreading(stage,self.admin)
        stage.refresh_from_db();self.assertFalse(stage.is_completed)
