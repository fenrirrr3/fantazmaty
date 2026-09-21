from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin
from core.testing_forms import post_form
from core.models import WorkflowEvent
from core.selectors.texts import available_stages_for_user
from core.workflow_events import event_scope, deliver
from core.services.texts import start_assigned_stage
from authors.models import Author
from texts.models import Text, Review, ReviewAssignment, Reviewers
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.services import claim_ready_for_editing, complete_stage

class WorkflowQueueAdminTests(CoreTestDataMixin, TestCase):
    def text(self, kind='ready_for_editing', assigned=False):
        text = Text.objects.create(title='Tekst kontroli', length=1000, anthology=self.anthology)
        text.authors.add(self.author)
        stage = WorkflowStage.objects.create(text=text, stage_type=kind)
        if assigned:
            WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=self.editor)
        return text, stage

    def test_finished_anthology_cannot_claim_start_complete_restart(self):
        text, stage = self.text()
        self.anthology.status = 'ready'; self.anthology.save()
        self.assertEqual(len(available_stages_for_user(user=self.editor)), 0)
        with self.assertRaises(ValidationError): claim_ready_for_editing(text, self.editor)
        with self.assertRaises(ValidationError): complete_stage(stage, self.superuser, timezone.localdate())
        with self.assertRaises(ValidationError): start_assigned_stage(user=self.superuser, stage_id=stage.pk, started_at=timezone.localdate())
        self.assertFalse(text.workflow_role_assignments.exists())

    def test_my_reviews_retains_empty_selected_anthology(self):
        review = Review.objects.create(title='Recenzja', anthology=self.anthology, length=1000, genre='fantasy', email='test@example.com')
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion='yes')
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:my_reviews'), {'view':'waiting','anthology':self.anthology.pk})
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertIn(self.anthology, response.context['anthologies'])

    def test_get_detail_has_no_row_locks_and_author_search_keeps_selection(self):
        text, _ = self.text()
        other = Author.objects.create(first_name='Drugi',last_name='Autor',email='other@example.com')
        self.client.force_login(self.superuser)
        url = reverse('core:assigned_text_detail', args=[text.pk])
        with patch('django.db.models.query.QuerySet.select_for_update', side_effect=AssertionError('GET acquired a row lock')):
            response = self.client.get(url)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'data-author-search-url=')
        self.assertEqual([a['pk'] for a in response.context['all_authors']], [self.author.pk])
        result = post_form(self.client, reverse('core:set_text_authors',args=[text.pk]), {'authors':[self.author.pk,other.pk]})
        self.assertEqual(result.status_code,302)
        self.assertEqual(set(text.authors.values_list('pk',flat=True)), {self.author.pk,other.pk})

    def event(self, **kwargs):
        return WorkflowEvent.objects.create(title='Test',authors='Autor',actor=self.superuser,actor_name='Test', previous_status='A',next_status='B',channel='Testowy', **kwargs)

    @override_settings(DISCORD_WEBHOOKS={'Testowy':'https://discord.com/api/webhooks/1/test'})
    @patch('core.discord_webhook.send_message', return_value='123')
    def test_web_sends_after_commit_once(self, send):
        text, _ = self.text()
        with self.captureOnCommitCallbacks(execute=True):
            with event_scope(self.superuser):
                WorkflowStage.objects.create(text=text,stage_type='editing',started_at=timezone.localdate())
            send.assert_not_called()
        event = WorkflowEvent.objects.get()
        self.assertEqual(event.status,'sent')
        deliver(event.pk)
        send.assert_called_once()
        event.refresh_from_db();self.assertEqual(event.status,'sent')
        self.assertIsNotNone(event.sending_started_at)

    @patch('core.discord_webhook.send_message')
    def test_abandoned_and_uncertain_attempts_never_auto_retry(self, send):
        event = self.event(status='sending', sending_started_at=timezone.now()-timedelta(hours=1))
        self.event(status='unknown')
        with self.assertRaises(CommandError):
            call_command('dispatch_workflow_notifications',watch=True,stdout=StringIO())
        event.refresh_from_db();self.assertEqual(event.status,'sending');send.assert_not_called()

    def test_admin_safe_notes_and_frozen_state(self):
        text, stage = self.text('editing',True)
        self.client.force_login(self.superuser)
        request = RequestFactory().get('/panel/');request.user=self.superuser
        for model,obj in [(WorkflowStage,stage),(WorkflowRoleAssignment,text.workflow_role_assignments.get())]:
            model_admin=admin.site._registry[model]
            form=model_admin.get_form(request,obj)(instance=obj)
            for field in ('text','workflow_cycle','stage_type','assigned_to','is_completed'):
                self.assertNotIn(field,form.fields)
            self.assertNotIn('set_cycle_as_current',model_admin.get_actions(request))
            response=self.client.get(reverse('admin:'+model._meta.app_label+'_'+model._meta.model_name+'_change',args=[obj.pk]))
            self.assertEqual(response.status_code,200)
        assignment=text.workflow_role_assignments.get()
        result=post_form(self.client,reverse('admin:workflow_workflowroleassignment_change',args=[assignment.pk]),{'notes':'Uwaga poprawiona','_save':'Zapisz'})
        self.assertEqual(result.status_code,302)
        assignment.refresh_from_db();self.assertEqual(assignment.notes,'Uwaga poprawiona')
        review=Review.objects.create(title='Recenzja',anthology=self.anthology,length=1000,genre='fantasy',email='test@example.com')
        form=admin.site._registry[Review].get_form(request,review)(instance=review)
        for field in ('status','old_reviews','copied_text','author_notified_at'):self.assertNotIn(field,form.fields)
        row=Reviewers.objects.create(review=review)
        result=post_form(self.client,reverse('admin:texts_reviewers_change',args=[row.pk]),{'general_notes':'Notatka','_save':'Zapisz'})
        self.assertEqual(result.status_code,302)
        row.refresh_from_db();self.assertEqual(row.general_notes,'Notatka')

    def test_report_links_to_team_profiles_without_reviewer_badges(self):
        review=Review.objects.create(title='Recenzja',anthology=self.anthology,length=1000,genre='fantasy',email='test@example.com')
        ReviewAssignment.objects.create(review=review,user=self.reviewer,position=1,opinion='yes')
        self.client.force_login(self.superuser)
        response=self.client.get(reverse('core:reviewer_activity'))
        self.assertContains(response,reverse('core:person_detail',args=[self.reviewer_person.pk]))
        self.assertNotContains(response,'data-palette="reviewer"')

    def test_admin_vacation_updates_profile_and_protects_cached_fields(self):
        from people.models import Person, Vacation
        now=timezone.now()
        vacation=Vacation(person=self.editor_person,start_date=timezone.localdate()+timedelta(days=2),end_date=now+timedelta(days=5))
        request=RequestFactory().post('/panel/');request.user=self.superuser
        vacation_admin=admin.site._registry[Vacation]
        vacation_admin.save_model(request,vacation,None,False)
        self.editor_person.refresh_from_db()
        self.assertEqual(self.editor_person.leave_start_date,vacation.start_date)
        self.assertEqual(self.editor_person.leave_end_date,vacation.end_date)
        person_admin=admin.site._registry[Person]
        form=person_admin.get_form(request,self.editor_person)(instance=self.editor_person)
        for field in ('leave_start_date','leave_end_date','leave_until_revoked','user'):
            self.assertNotIn(field,form.fields)
        self.assertIn('email',form.fields)
        self.assertIn('dropbox_email',form.fields)
        self.assertTrue(vacation_admin.has_delete_permission(request,vacation))
        vacation_admin.delete_model(request,vacation)
        self.editor_person.refresh_from_db()
        self.assertIsNone(self.editor_person.leave_start_date)

    def test_review_inline_keeps_archive_editable_but_current_state_readonly(self):
        from texts.admin import ReviewAssignmentInline
        inline=ReviewAssignmentInline(Review,admin.site)
        request=RequestFactory().get('/panel/');request.user=self.superuser
        review=Review(title='Archiwum',old_reviews=True)
        self.assertNotIn('opinion',inline.get_readonly_fields(request,review))
        self.assertTrue(inline.has_add_permission(request,review))
        review.old_reviews=False
        self.assertIn('opinion',inline.get_readonly_fields(request,review))
        self.assertFalse(inline.has_add_permission(request,review))
        self.assertNotIn('notes',inline.get_readonly_fields(request,review))
