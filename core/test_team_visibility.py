from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from core.selectors.texts import text_list_context, _assignment_data, _stage_data
from core.views.search import _search_people
from people.models import Person
from texts.models import Text
from workflow.labels import assignment_label
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class TeamVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin', 'admin@example.com', 'test-password')
        cls.member = User.objects.create_user('member', 'member@example.com', first_name='Jan', last_name='Aktywny')
        cls.member_profile = Person.objects.create(user=cls.member,first_name='Jan',last_name='Aktywny',email=cls.member.email)
        cls.disabled = User.objects.create_user('hidden@example.com', 'hidden@example.com', is_active=False,
                                               first_name='Adam',last_name='Nieaktywny')
        cls.disabled_profile = Person.objects.create(user=cls.disabled,first_name='Adam',last_name='Nieaktywny',
            email=cls.disabled.email,dropbox_email='hidden-dropbox@example.com')

    def test_inactive_account_is_absent_from_team_and_search(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:people_list'))
        self.assertNotContains(response, self.disabled.email)
        self.assertNotContains(response, 'Nieaktywny')
        self.assertContains(response, self.member.email)
        self.assertEqual(_search_people('Nieaktywny', self.admin), [])
        self.assertEqual(_search_people(self.disabled.email, self.admin), [])

    def test_inactive_contact_is_hidden_in_profile_for_member_and_superuser(self):
        for viewer in (self.member, self.admin):
            self.client.force_login(viewer)
            response = self.client.get(reverse('core:person_detail',args=[self.disabled_profile.pk]))
            self.assertEqual(response.status_code,200)
            self.assertNotContains(response,self.disabled.email)
            self.assertNotContains(response,self.disabled_profile.dropbox_email)
        response = self.client.get(reverse('core:person_permissions',args=[self.disabled_profile.pk]))
        self.assertNotContains(response,self.disabled.email)
        response = self.client.get(reverse('admin:people_person_change',args=[self.disabled_profile.pk]))
        self.assertContains(response,self.disabled.email)
        self.assertContains(response,self.disabled_profile.dropbox_email)

    def test_active_profile_without_account_is_not_hidden(self):
        person=Person.objects.create(first_name='Maria',last_name='BezKonta',email='no-account@example.com')
        self.assertTrue(Person.objects.active().filter(pk=person.pk).exists())
        self.assertTrue(person.can_show_team_contact)
        person.is_active=False
        person.save()
        self.assertFalse(person.can_show_team_contact)
        self.assertFalse(Person.objects.active().filter(pk=person.pk).exists())

    def test_hide_ready_hides_withdrawn_and_ready_but_not_other_texts(self):
        rows={}
        for kind in ('ready', 'withdrawn', 'ready_for_editing'):
            text=Text.objects.create(title=kind,length=100)
            WorkflowStage.objects.create(text=text,stage_type=kind)
            rows[kind]=text.pk
        no_stage=Text.objects.create(title='Bez etapu',length=100)
        def ids(params):
            return set(text_list_context(user=self.admin,params=QueryDict(params))['filtered_queryset'].values_list('pk',flat=True))
        self.assertEqual(ids('hide_ready=1'),{rows['ready_for_editing'],no_stage.pk})
        self.assertEqual(ids('hide_ready=0'),set(rows.values())|{no_stage.pk})
        self.assertEqual(ids('hide_ready=1&status=withdrawn'),set())
        self.assertEqual(ids('hide_ready=1&status=ready'),set())
        self.assertEqual(ids('hide_ready=0&status=withdrawn'),{rows['withdrawn']})

    def test_short_execution_labels(self):
        for role, label, stage_kind in (
            ('editor','Redakcja','editing'),
            ('proofreader_1','Pierwsza korekta','first_proofreading'),
            ('verifier_2','Druga weryfikacja','second_verification'),
        ):
            assignment=WorkflowRoleAssignment(role=role,execution_number=2)
            self.assertEqual(_assignment_data(assignment)['get_role_display'],label+' (wyk. 2)')
            self.assertEqual(_stage_data(WorkflowStage(stage_type=stage_kind,execution_number=2))['get_stage_type_display'],label+' (wyk. 2)')
            assignment.execution_number=1
            self.assertEqual(assignment_label(assignment,show_first=True),label+' (wyk. 1)')
