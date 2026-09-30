from email.message import EmailMessage
import html
import re
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError, PermissionDenied
from django.urls import reverse
from django.utils import timezone
from authors.models import Author
from people.models import Person, Role
from texts.models import Text, Anthology, Review
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.admin_performers import correct_stage_performers
from workflow.admin_assignment_edit import correct_assignment
from workflow.admin_add_stage import add_missing_stage
from core.edit_versions import version_of
from core.models import MailboxConnection, MailboxDownload
from core.services.mailbox_import import parse_message, mailbox_key
from core.services.mailbox import MailboxError


class AdminPolicyRepairs(TestCase):
    def setUp(self):
        User=get_user_model()
        self.admin=User.objects.create_superuser('admin','admin@example.com','test')
        self.first=User.objects.create_superuser('first','first@example.com','test')
        self.later=User.objects.create_superuser('later','later@example.com','test')
        self.ordinary=User.objects.create_user('ordinary',email='ordinary@example.com')
        person=Person.objects.create(user=self.ordinary,first_name='Jan',last_name='Korektor',email=self.ordinary.email,is_active=True)
        role,_=Role.objects.get_or_create(name='Korektor');person.roles.add(role)
        self.book=Anthology.objects.create(title='Test')
        self.text=Text.objects.create(title='Tekst',length=100,anthology=self.book)
        author=Author.objects.create(first_name='Jan',last_name='Autor',email='author@example.com')
        self.text.authors.add(author)
        self.today=timezone.localdate()
        self.client.force_login(self.admin)

    def pair(self, kind='second_proofreading'):
        a1=A.objects.create(text=self.text,role='proofreader_1',assigned_to=self.first)
        a2=A.objects.create(text=self.text,role='proofreader_2' if kind=='second_proofreading' else 'proofreader_3',assigned_to=self.later)
        s1=S.objects.create(text=self.text,stage_type='first_proofreading',assignment=a1,
                           started_at=self.today,ended_at=self.today,is_completed=True)
        s2=S.objects.create(text=self.text,stage_type=kind,assignment=a2,started_at=self.today)
        return a1,a2,s1,s2

    def test_batch_cannot_assign_first_and_later_to_same_person(self):
        for kind in ('second_proofreading','third_proofreading'):
            with self.subTest(kind=kind):
                # Each example has its own workflow.
                if kind=='third_proofreading':
                    self.text=Text.objects.create(title='Drugi tekst',length=100,anthology=self.book)
                a1,a2,s1,s2=self.pair(kind)
                with self.assertRaises(ValidationError):
                    correct_stage_performers(self.text.pk,{s1.pk:self.admin,s2.pk:self.admin},self.admin,version_of(self.text))
                a1.refresh_from_db();a2.refresh_from_db()
                self.assertEqual(a1.assigned_to_id,self.first.pk);self.assertEqual(a2.assigned_to_id,self.later.pk)

    def test_batch_can_swap_without_false_conflict_with_old_data(self):
        a1,a2,s1,s2=self.pair()
        correct_stage_performers(self.text.pk,{s1.pk:self.later,s2.pk:self.first},self.admin,version_of(self.text))
        a1.refresh_from_db();a2.refresh_from_db()
        self.assertEqual(a1.assigned_to_id,self.later.pk);self.assertEqual(a2.assigned_to_id,self.first.pk)

    def test_changing_completed_first_cannot_conflict_with_current_later(self):
        a1,a2,s1,s2=self.pair()
        for edit in (
            lambda:correct_stage_performers(self.text.pk,{s1.pk:self.later},self.admin,version_of(self.text)),
            lambda:correct_assignment(a1.pk,self.admin,version_of(self.text),action='performer',performer=self.later),
        ):
            with self.assertRaises(ValidationError):edit()
        a1.refresh_from_db();self.assertEqual(a1.assigned_to_id,self.first.pk)

    def close_book(self, s2):
        s2.ended_at=self.today;s2.is_completed=True;s2.save()
        S.objects.create(text=self.text,stage_type='ready',started_at=self.today,ended_at=self.today,is_completed=True)
        self.book.status='ready';self.book.save()

    def test_closed_anthology_blocks_inline_and_assignment_operations(self):
        a1,a2,s1,s2=self.pair();self.close_book(s2)
        operations=(
            lambda:correct_stage_performers(self.text.pk,{s1.pk:self.admin},self.admin,version_of(self.text)),
            lambda:correct_assignment(a1.pk,self.admin,version_of(self.text),action='performer',performer=self.admin),
            lambda:correct_assignment(a1.pk,self.admin,version_of(self.text),action='clear'),
        )
        for operation in operations:
            with self.assertRaises(ValidationError):operation()
        a1.refresh_from_db();self.assertEqual(a1.assigned_to_id,self.first.pk)

    def inline_payload(self, changes):
        url=reverse('admin:texts_text_change',args=[self.text.pk])
        response=self.client.get(url)
        form=response.context['adminform'].form
        data={name:form[name].value() for name in form.fields if form[name].value() is not None}
        data.update(authors=list(self.text.authors.values_list('pk',flat=True)),_save='Save',
                    _edit_version=html.unescape(re.search(r'name="_edit_version" value="([^"]+)"',response.content.decode()).group(1)))
        for inline in response.context['inline_admin_formsets']:
            fs=inline.formset;prefix=fs.prefix
            if fs.model is S:
                data[prefix+'-TOTAL_FORMS']=str(fs.total_form_count())
                data[prefix+'-INITIAL_FORMS']=str(fs.initial_form_count())
                for i,subform in enumerate(fs.forms):
                    stage=subform.instance
                    data[f'{prefix}-{i}-id']=str(stage.pk)
                    data[f'{prefix}-{i}-text']=str(self.text.pk)
                    data[f'{prefix}-{i}-performer']=str(changes.get(stage.pk,stage.assignment.assigned_to_id if stage.assignment else '') or '')
                    data[f'{prefix}-{i}-workflow_version']=str(version_of(self.text))
            else:
                data[prefix+'-TOTAL_FORMS']='0';data[prefix+'-INITIAL_FORMS']='0'
        return url,data

    def test_admin_correction_accepts_person_without_current_role(self):
        a1,a2,s1,s2=self.pair()
        self.ordinary.person_profile.roles.clear()
        url,data=self.inline_payload({s2.pk:self.ordinary.pk})
        response=self.client.post(url,data)
        self.assertEqual(response.status_code,302)
        a2.refresh_from_db();self.assertEqual(a2.assigned_to_id,self.ordinary.pk)

    def test_closed_anthology_inline_post_reports_form_error(self):
        a1,a2,s1,s2=self.pair();self.close_book(s2)
        url,data=self.inline_payload({s1.pk:self.admin.pk})
        response=self.client.post(url,data)
        self.assertEqual(response.status_code,200);self.assertContains(response,'Antologia jest gotowa')
        a1.refresh_from_db();self.assertEqual(a1.assigned_to_id,self.first.pk)

    def test_assignment_correction_accepts_former_role(self):
        a1,a2,s1,s2=self.pair()
        response=self.client.post(reverse('admin:workflow_assignment_correct',args=[a2.pk]),{
            'action':'performer','performer':self.ordinary.pk,'confirm':'on','version':version_of(self.text)})
        self.assertEqual(response.status_code,302)
        a2.refresh_from_db();self.assertEqual(a2.assigned_to_id,self.ordinary.pk)

    def test_add_stage_unsuitable_person_is_validation_error(self):
        with self.assertRaises(ValidationError):
            add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='second_proofreading',performer=self.ordinary)
        self.assertFalse(S.objects.filter(text=self.text).exists())

    def test_authorization_denial_for_actor_is_preserved(self):
        a1,a2,s1,s2=self.pair()
        with self.assertRaises(PermissionDenied):
            correct_stage_performers(self.text.pk,{s2.pk:self.admin},self.ordinary,version_of(self.text))

    def test_imported_dates_cannot_be_edited_in_closed_anthology(self):
        from workflow.import_context import importing_completed
        a1,a2,s1,s2=self.pair();self.close_book(s2)
        token=importing_completed.set(True)
        try:s1.imported_completed=True;s1.save()
        finally:importing_completed.reset(token)
        url=reverse('admin:workflow_workflowstage_change',args=[s1.pk])
        page=self.client.get(url)
        token=html.unescape(re.search(r'name="_edit_version" value="([^"]+)"',page.content.decode()).group(1))
        response=self.client.post(url,{
            '_edit_version':token,'started_at':'2026-01-01','ended_at':'2026-01-02','confirm_data_correction':'on','_save':'Save'})
        self.assertEqual(response.status_code,200);self.assertContains(response,'Antologia jest gotowa')
        s1.refresh_from_db();self.assertEqual(s1.started_at,self.today)

    def html_mail(self):
        message=EmailMessage();message['Subject']='Test';message.set_content('');message.set_type('text/html')
        message.add_attachment(b'notempty',maintype='application',subtype='octet-stream',filename='tekst.docx')
        return message.as_bytes()

    def test_empty_html_is_readable_mailbox_error(self):
        with self.assertRaisesMessage(MailboxError,'treść HTML jest pusta'):
            parse_message(1,self.html_mail())

    def test_empty_html_endpoint_does_not_create_records(self):
        box=MailboxConnection.objects.create(name='teksty',host='imap.example.com',username='teksty@example.com',encrypted_password='unused')
        selection=signing.dumps({'user':self.admin.pk,'mailbox':mailbox_key(box),'validity':7,'uids':[12]},salt='mailbox-selection')
        raw=self.html_mail()
        with patch('core.views.mailbox.fetch_messages',side_effect=lambda *args: [parse_message(12,raw)]):
            response=self.client.post(reverse('core:review_bulk_import'),{'action':'download','selection':selection,'uids':['12']})
        self.assertEqual(response.status_code,400);self.assertContains(response,'treść HTML jest pusta',status_code=400)
        self.assertEqual(Review.objects.count(),0);self.assertEqual(MailboxDownload.objects.count(),0)
