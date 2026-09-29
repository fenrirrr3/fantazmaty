from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.urls import reverse
from django.utils import timezone
from people.models import Person, Role, Vacation
from texts.models import Text, Anthology
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.handoffs import handoff_stage, eligible_handoff_users
from workflow.admin_performers import correct_stage_performers
from workflow.admin_add_stage import add_missing_stage
from core.edit_versions import version_of
from core.models import MailboxConnection, MailboxDownload, NewsletterConsent
from core.services.mailbox_import import parse_message, prepare_forms, mailbox_key, fetch_messages


class WorkflowChanges(TestCase):
    def setUp(self):
        U = get_user_model()
        self.admin = U.objects.create_superuser('admin', 'admin@example.test', 'test')
        self.users=[]
        for i in range(2):
            u=U.objects.create_user('u'+str(i),first_name='Osoba',last_name=str(i))
            p=Person.objects.create(user=u,first_name='Osoba',last_name=str(i),email=f'u{i}@example.test',is_active=True)
            for name in ['Redaktor','Korektor','Weryfikator']:
                r,_=Role.objects.get_or_create(name=name);p.roles.add(r)
            self.users.append(u)
        self.text=Text.objects.create(title='Test',length=100)
        self.today=timezone.localdate()

    def stage(self,kind,role,user,**kw):
        a=A.objects.create(text=self.text,role=role,assigned_to=user)
        return S.objects.create(text=self.text,stage_type=kind,assignment=a,**kw)

    def test_first_proofreader_handoff_and_admin_blocked(self):
        a,b=self.users
        self.stage('first_proofreading','proofreader_1',b,started_at=self.today,ended_at=self.today,is_completed=True)
        s=self.stage('third_proofreading','proofreader_3',a,started_at=self.today)
        self.assertFalse(eligible_handoff_users(s).filter(pk=b.pk).exists())
        with self.assertRaises(ValidationError):handoff_stage(self.text,self.admin,stage_id=s.pk,assigned_to_id=b.pk,expected_assignment_id=s.assignment_id,reason='Test')
        with self.assertRaises(ValidationError):correct_stage_performers(self.text.pk,{s.pk:b},self.admin,version_of(self.text))
        s.assignment.refresh_from_db();self.assertEqual(s.assignment.assigned_to_id,a.pk)

    def test_swap_verifiers_atomically(self):
        a,b=self.users
        s1=self.stage('first_verification','verifier_1',a)
        s2=self.stage('second_verification','verifier_2',b)
        correct_stage_performers(self.text.pk,{s1.pk:b,s2.pk:a},self.admin,version_of(self.text))
        s1.assignment.refresh_from_db();s2.assignment.refresh_from_db()
        self.assertEqual(s1.assignment.assigned_to_id,b.pk);self.assertEqual(s2.assignment.assigned_to_id,a.pk)
        with self.assertRaises(ValidationError):correct_stage_performers(self.text.pk,{s2.pk:b},self.admin,version_of(self.text))
        s2.assignment.refresh_from_db();self.assertEqual(s2.assignment.assigned_to_id,a.pk)

    def test_candidates_query_count_and_leave(self):
        a,b=self.users;s=self.stage('editing','editor',a)
        with self.assertNumQueries(1):self.assertIn(b.pk,list(eligible_handoff_users(s).values_list('pk',flat=True)))
        Vacation.objects.create(person=b.person_profile,start_date=self.today,until_revoked=True)
        self.assertFalse(eligible_handoff_users(s).filter(pk=b.pk).exists())

    def test_add_missing_pending_with_performer(self):
        s=add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='editing',performer=self.users[0])
        self.assertEqual(s.assignment.assigned_to_id,self.users[0].pk)
        self.assertIsNone(s.started_at);self.assertFalse(s.is_completed)
        with self.assertRaises(ValidationError):add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='first_proofreading',performer=self.users[1])

    def test_add_completed_missing_does_not_change_open_stage(self):
        open_stage=self.stage('third_proofreading','proofreader_3',self.users[0])
        s=add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='first_proofreading',performer=self.users[1],started_at=self.today,ended_at=self.today)
        self.assertTrue(s.is_completed)
        open_stage.refresh_from_db();self.assertTrue(open_stage.is_current);self.assertFalse(open_stage.is_completed)
        with self.assertRaises(ValidationError):add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='first_proofreading',performer=self.users[1],started_at=self.today,ended_at=self.today)

    def test_stage_permissions_ready_anthology_dates_and_version(self):
        with self.assertRaises(PermissionDenied):add_missing_stage(self.text.pk,self.users[0],version_of(self.text),kind='editing')
        with self.assertRaises(ValidationError):add_missing_stage(self.text.pk,self.admin,-1,kind='editing')
        with self.assertRaises(ValidationError):add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='editing',performer=self.users[0],started_at=self.today,ended_at=self.today-timedelta(days=1))
        self.assertFalse(S.objects.filter(text=self.text).exists())
        book=Anthology.objects.create(title='Published',status='ready')
        Text.objects.filter(pk=self.text.pk).update(anthology=book)
        with self.assertRaises(ValidationError):add_missing_stage(self.text.pk,self.admin,version_of(self.text),kind='editing')

    def test_note_timestamp_create_edit_clear(self):
        self.text.coordinator_note='Notatka';self.text.save(update_fields=['coordinator_note'])
        self.text.refresh_from_db();stamp=self.text.coordinator_note_updated_at
        self.assertIsNotNone(stamp)
        self.text.title='Nowy';self.text.save();self.text.refresh_from_db();self.assertEqual(self.text.coordinator_note_updated_at,stamp)
        self.text.coordinator_note='';self.text.save(update_fields=['coordinator_note']);self.text.refresh_from_db();self.assertIsNone(self.text.coordinator_note_updated_at)

    def test_admin_add_page_and_newsletter_copy(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('admin:texts_text_add_stage',args=[self.text.pk])),'Dodaj etap')
        response=self.client.post(reverse('admin:texts_text_add_stage',args=[self.text.pk]),{'kind':'editing','performer':self.users[0].pk,'version':version_of(self.text)})
        self.assertEqual(response.status_code,302)
        NewsletterConsent.objects.create(email='test@example.test',premieres=True)
        response=self.client.get(reverse('core:newsletter_list'),{'page_size':250})
        self.assertContains(response,'Kopiuj adresy z tej strony');self.assertContains(response,'data-copy-table')
        self.assertEqual(response.context['page_obj'].paginator.per_page,250)

    def test_summary_editors_only_and_anthology_link(self):
        self.stage('editing','editor',self.users[0]);self.stage('first_verification','verifier_1',self.users[1])
        self.client.force_login(self.admin)
        response=self.client.get(reverse('core:workflow_list'))
        self.assertContains(response,'Redaktor');self.assertNotContains(response,'Stan etapu')
        self.assertEqual(response.context['role_columns'],[('editor','Redaktor')])
        book=Anthology.objects.create(title='Antologia')
        self.assertContains(self.client.get(reverse('core:anthology_detail',args=[book.pk])),reverse('core:text_list')+'?anthology='+str(book.pk)+'&amp;hide_ready=0')


class MailIdentity(TestCase):
    def setUp(self):
        self.box=MailboxConnection.objects.create(name='teksty',host='imap.example.test',username='teksty@example.test')
        self.box.set_password('test');self.box.save()
        self.admin=get_user_model().objects.create_superuser('admin','admin@example.test','test')

    def raw(self,body='nieobsługiwany stary formularz',received=None):
        m=EmailMessage();m['From']='a@example.test';m['Subject']='Autor – Tytuł';m['Message-ID']='<stable@example.test>'
        if received:m['Received']=received
        m.set_content(body);m.add_attachment(b'original',maintype='application',subtype='octet-stream',filename='tekst.docx')
        return m.as_bytes()

    def test_identity_survives_transport_headers_but_not_content_change(self):
        a=parse_message(1,self.raw(),submission=False);b=parse_message(2,self.raw(received='new server'),submission=False)
        self.assertEqual(a['fingerprint'],b['fingerprint'])
        self.assertNotEqual(a['fingerprint'],parse_message(2,self.raw(body='different'),submission=False)['fingerprint'])

    @patch('core.services.mailbox_import.imaplib.IMAP4_SSL')
    def test_redownload_after_folder_change_without_parsing_submission(self,mock):
        raw=self.raw();meta=parse_message(1,raw,submission=False)
        MailboxDownload.objects.create(mailbox_key='old',uid_validity=1,uid=1,fingerprint=meta['fingerprint'])
        client=mock.return_value;client.select.return_value=('OK',[]);client.response.return_value=('UIDVALIDITY',[b'2'])
        client.uid.side_effect=[('OK',[f'1 (UID 9 RFC822.SIZE {len(raw)})'.encode()]),('OK',[(b'1 (UID 9 BODY[] {})',raw)])]
        rows=fetch_messages(self.box,2,[9])
        self.assertEqual(prepare_forms(rows,self.box,2,self.admin),[])
        self.assertTrue(rows[0]['downloaded']);self.assertEqual(rows[0]['files'][0][1],b'original')
        self.assertFalse(client.store.called);self.assertFalse(client.expunge.called)

    def test_identical_messages_with_two_uids_import_once(self):
        Anthology.objects.create(title='Nabór')
        raw=self.raw(body='Jan Kowalski;Tytuł;fantasy;100;a@example.test;123456789;Nabór')
        rows=[parse_message(i,raw) for i in (1,2)]
        forms=prepare_forms(rows,self.box,1,self.admin)
        self.assertEqual(len(forms),1);self.assertTrue(rows[1]['downloaded'])
        self.assertEqual(forms[0].data['records'].count('\n'),0)
