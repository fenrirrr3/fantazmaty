import re
from io import StringIO
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import Group
from django.urls import reverse,resolve
from core.tests import CoreTestDataMixin
from core.middleware import fingerprint, aggregate
from core.models import AnthologyCorrection
from core.selectors.texts import available_stages_for_user
from core.permissions import is_coordinator
from people.models import Role
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from authors.models import Author

class Logic11Tests(CoreTestDataMixin,TestCase):
    def setUp(self):
        self.text=Text.objects.create(title='Test',length=100,anthology=self.anthology)
        self.client.force_login(self.superuser)

    def token(self,url):
        response=self.client.get(url)
        match=re.search(r'name="_edit_version" value="([^"]+)"',response.content.decode())
        self.assertIsNotNone(match,response.status_code)
        return match.group(1)

    def test_authors_are_versioned_and_missing_token_rejected(self):
        self.text.authors.add(self.author)
        detail=reverse('core:assigned_text_detail',args=[self.text.pk])
        token=self.token(detail)
        old=fingerprint(self.text)
        second=Author.objects.create(first_name='Inny',last_name='Autor',email='inny@example.com')
        self.text.authors.add(second)
        self.assertNotEqual(old,fingerprint(self.text))
        url=reverse('core:set_text_authors',args=[self.text.pk])
        for data in ({'authors':[self.author.pk]}, {'authors':[self.author.pk],'_edit_version':token}):
            self.assertEqual(self.client.post(url,data).status_code,409)
        self.assertEqual(self.text.authors.count(),2)
        self.assertEqual(self.client.post(url,{'authors':[self.author.pk],'_edit_version':self.token(detail)}).status_code,302)
        self.assertEqual(self.text.authors.count(),1)

    def test_admin_workflow_aggregate_is_parent_text(self):
        s=S.objects.create(text=self.text,stage_type='ready_for_editing')
        a=A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        for name,obj in [('workflowstage',s),('workflowroleassignment',a)]:
            match=resolve(reverse('admin:workflow_'+name+'_change',args=[obj.pk]))
            self.assertEqual(aggregate(match),(Text,self.text.pk))

    def test_removing_last_role_revokes_all_coordinator_sources(self):
        p=self.coordinator_person
        role=Role.objects.get_or_create(name='Koordynator redakcji')[0]
        p.roles.add(role)
        group=Group.objects.create(name='Koordynator korekty')
        self.coordinator.groups.add(group)
        self.assertTrue(is_coordinator(self.coordinator))
        p.roles.remove(role)
        p.refresh_from_db();self.coordinator.refresh_from_db()
        self.assertFalse(p.is_coordinator)
        self.assertFalse(is_coordinator(self.coordinator))
        self.assertFalse(self.coordinator.is_staff)
        self.assertFalse(self.coordinator.groups.filter(pk=group.pk).exists())

    def test_removing_one_of_two_coordinator_roles_keeps_access(self):
        p=self.coordinator_person
        r1=Role.objects.get_or_create(name='Koordynator redakcji')[0]
        r2=Role.objects.get_or_create(name='Koordynator korekty')[0]
        p.roles.add(r1,r2);p.roles.remove(r1)
        self.assertTrue(is_coordinator(self.coordinator))
        p.roles.clear();self.assertFalse(is_coordinator(self.coordinator))

    def test_group_clear_and_flag_revocation(self):
        p=self.coordinator_person
        p.roles.add(Role.objects.get_or_create(name='Koordynator redakcji')[0])
        self.coordinator.groups.add(Group.objects.create(name='Koordynator'))
        self.coordinator.groups.clear()
        self.assertFalse(is_coordinator(self.coordinator))
        p.refresh_from_db();p.is_coordinator=True;p.save()
        p.is_coordinator=False;p.save()
        self.assertFalse(is_coordinator(self.coordinator))

    def test_claim_query_count_does_not_grow_with_texts(self):
        for count in (2,10):
            for _ in range(count):
                t=Text.objects.create(title='Test listy',length=100)
                S.objects.create(text=t,stage_type='ready_for_editing')
            with CaptureQueriesContext(connection) as queries:
                available_stages_for_user(user=self.editor)
            if count==2:small=len(queries)
            else:self.assertLessEqual(len(queries),small+2)

    def test_resolved_corrections_cannot_be_changed_or_deleted(self):
        self.anthology.status='ready';self.anthology.save()
        for status in ('applied','rejected'):
            item=AnthologyCorrection.objects.create(anthology=self.anthology,text=self.text,story_title=self.text.title,fragment='a',problem='b',suggestion='c',submitted_by=self.superuser,status=status)
            edit=reverse('core:correction_edit',args=[item.pk]);delete=reverse('core:correction_delete',args=[item.pk])
            self.assertEqual(self.client.get(edit).status_code,403)
            self.assertEqual(self.client.post(edit,{'version':item.updated_at.isoformat()}).status_code,403)
            self.assertEqual(self.client.post(delete,{'version':item.updated_at.isoformat()}).status_code,403)
            self.assertTrue(AnthologyCorrection.objects.filter(pk=item.pk,status=status).exists())
            self.assertNotContains(self.client.get(reverse('core:anthology_corrections')),edit)

    def test_role_rename_and_delete_revoke_access(self):
        p=self.coordinator_person
        role=Role.objects.create(name='Koordynator testowy')
        p.roles.add(role)
        role.name='Pomocnik';role.save()
        self.assertFalse(is_coordinator(self.coordinator))
        role.name='Koordynator testowy';role.save()
        self.assertTrue(is_coordinator(self.coordinator))
        role.delete()
        self.assertFalse(is_coordinator(self.coordinator))

    def test_admin_cannot_edit_resolved_correction(self):
        from django.contrib import admin
        from django.test import RequestFactory
        item=AnthologyCorrection.objects.create(anthology=self.anthology,story_title='Test',fragment='a',problem='b',suggestion='c',submitted_by=self.superuser,status='applied')
        model_admin=admin.site._registry[AnthologyCorrection]
        request=RequestFactory().get('/');request.user=self.superuser
        self.assertFalse(model_admin.has_change_permission(request,item))
        self.assertFalse(model_admin.has_delete_permission(request,item))

    def test_admin_missing_version_is_rejected_and_current_version_saves(self):
        from core.testing_forms import post_form
        stage=S.objects.create(text=self.text,stage_type='ready_for_editing')
        url=reverse('admin:workflow_workflowstage_change',args=[stage.pk])
        data={'text':self.text.pk,'workflow_cycle':1,'stage_type':'editing','iteration':1,'confirm_data_correction':'on'}
        self.assertEqual(self.client.post(url,data).status_code,409)
        self.assertEqual(post_form(self.client,url,data).status_code,302)
        stage.refresh_from_db();self.assertEqual(stage.stage_type,'editing')
