from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from authors.models import Author
from texts.models import Text, Review, Anthology
from people.models import Person
from core.models import WorkflowEvent, UserActivity
from core.activity import UserActivityMiddleware
from core.forms import ReviewBulkImportForm
from core.services.reviews import import_reviews

class Fixes25Tests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        override=self.settings(ACTIVITY_SPOOL_DIR=self.tmp.name);override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_superuser('admin','admin@example.com','testpassword')
        self.client.force_login(self.user)
        self.book=Anthology.objects.create(title='Open')
        self.author=Author.objects.create(first_name='Anna',last_name='Współautorka',email='co@example.com')
        self.review=Review.objects.create(author_first_name='Jan',author_last_name='Autor',email='main@example.com',title='Test',length=1000,anthology=self.book)

    def test_popup_requires_saved_review_and_blocks_duplicate(self):
        endpoint=reverse('admin:texts_review_prepare_text')
        self.assertEqual(self.client.post(endpoint,{}).status_code,400)
        result=self.client.post(endpoint,{'review_id':self.review.pk,'title':'Test'}).json()
        text=Text.objects.create(title='Already exists',length=1000)
        self.review.copied_text=text;self.review.save(update_fields=['copied_text'])
        self.assertEqual(self.client.get(result['url']).status_code,400)
        self.assertEqual(self.client.post(result['url'],{'title':'Duplicate'}).status_code,400)
        self.assertEqual(Text.objects.count(),1)

    def test_popup_full_save_keeps_both_authors_and_links_immediately(self):
        result=self.client.post(reverse('admin:texts_review_prepare_text'),{'review_id':self.review.pk,'title':'Test','length':'1000','anthology':self.book.pk,'source_author_first_name':'Jan','source_author_last_name':'Autor','source_author_email':'main@example.com','authors':[self.author.pk]}).json()
        response=self.client.get(result['url'])
        form=response.context['adminform'].form
        data={name:form[name].value() for name in form.fields if form[name].value() is not None}
        data.update({'_popup':'1','_save':'Save'})
        for inline in response.context['inline_admin_formsets']:
            for field in inline.formset.management_form:
                data[field.html_name]=field.value()
        response=self.client.post(result['url'],data)
        self.review.refresh_from_db()
        self.assertIsNotNone(self.review.copied_text_id)
        self.assertEqual(set(self.review.copied_text.authors.values_list('email',flat=True)),{'main@example.com','co@example.com'})
        self.assertEqual(self.client.post(result['url'],data).status_code,400)
        self.assertEqual(Text.objects.count(),1)

    def test_edit_blacklisted_coauthor_hides_unresolved_review(self):
        self.author.is_blacklisted=True;self.author.save()
        request=RequestFactory().post('/');request.user=self.user
        from types import SimpleNamespace
        form=SimpleNamespace(changed_data=['coauthors'],cleaned_data={'coauthors':[self.author]})
        admin.site._registry[Review].save_model(request,self.review,form,True)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status,Review.Status.REJECTED)
        self.assertTrue(self.review.is_hidden)

    def test_preparation_only_for_bulk_choices_and_forged_post(self):
        ready=Anthology.objects.create(title='Ready',status=Anthology.Status.READY)
        self.assertEqual(list(ReviewBulkImportForm(user=self.user).fields['anthology'].queryset),[self.book])
        invalid=ReviewBulkImportForm({'anthology':ready.pk,'records':'JAN AUTOR;Title;fantasy;1000;;mail@example.com;'},user=self.user)
        with self.assertRaises(ValidationError):import_reviews(user=self.user,form=invalid)
        self.assertEqual(Review.objects.count(),1)

    def test_logging_no_database_and_idempotent_flush(self):
        request=RequestFactory().post('/');request.user=self.user;request.session={};request._cms_activity=('Zapis','')
        with patch('core.workflow_events.run_admin_request',return_value=HttpResponse('Invalid form',status=200)):
            with self.assertNumQueries(0):UserActivityMiddleware(lambda r:HttpResponse())(request)
        file=next(Path(self.tmp.name).glob('*.json'));original=file.read_bytes()
        call_command('flush_activity',stdout=StringIO())
        self.assertTrue(UserActivity.objects.get().action.startswith('Próba / formularz:'))
        file.write_bytes(original)
        call_command('flush_activity',stdout=StringIO())
        self.assertEqual(UserActivity.objects.count(),1)

    def test_work_and_login_separate(self):
        member=get_user_model().objects.create_user('member',last_login=timezone.now())
        person=Person.objects.create(user=member,first_name='Osoba',last_name='Test',email='member@example.com')
        event=WorkflowEvent.objects.create(actor=member,actor_name='Test',title='Tekst',previous_status='Redakcja',next_status='Redakcja',channel='',personal_work=True,personal_work_description='Przejęcie: Redakcja')
        WorkflowEvent.objects.create(actor=member,actor_name='Test',title='Tekst',previous_status='A',next_status='B',channel='',personal_work=False)
        UserActivity.objects.create(user=member,actor='x',method='GET',action='Pulpit',status_code=200)
        url=reverse('core:last_activity')
        work=self.client.get(url,{'mode':'work','days':0})
        self.assertEqual(next(p for p in work.context['people'] if p.pk==person.pk).last_activity_at,event.created_at)
        login=self.client.get(url,{'mode':'login','days':0})
        self.assertEqual(next(p for p in login.context['people'] if p.pk==person.pk).last_activity_at,member.last_login)
        self.assertNotContains(login,'Przejęcie: Redakcja')
        self.client.force_login(member)
        self.assertEqual(self.client.get(url).status_code,403)

    def test_actual_workflow_claim_and_completion_recorded(self):
        from workflow.tests import create_member
        from workflow.models import WorkflowStage
        from workflow.services import claim_ready_for_editing,send_to_first_verification
        editor=create_member('editor','Redaktor')
        text=Text.objects.create(title='Work',length=1000,anthology=self.book)
        WorkflowStage.objects.create(text=text,stage_type=WorkflowStage.StageType.READY_FOR_EDITING)
        claim_ready_for_editing(text,editor)
        self.assertTrue(WorkflowEvent.objects.filter(actor=editor,personal_work=True,personal_work_description__startswith='Przejęcie:').exists())
        send_to_first_verification(text,editor)
        self.assertTrue(WorkflowEvent.objects.filter(actor=editor,personal_work=True,personal_work_description__startswith='Zakończenie:').exists())
