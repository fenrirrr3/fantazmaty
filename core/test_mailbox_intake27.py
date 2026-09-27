from unittest.mock import MagicMock, patch
from django.test import TestCase, SimpleTestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from core.admin import MailboxConnectionForm
from core.models import MailboxConnection
from core.services.mailbox import read_headers, MailboxError
from core.services.vacations import create_vacation
from texts.models import Anthology, Review
from people.models import Person, Role, Vacation


class MailboxProtocolTests(SimpleTestCase):
    def box(self, **kwargs):
        box=MailboxConnection(name='Test',host='imap.example.com',username='test@example.com',**kwargs)
        box.set_password('secret-not-in-html')
        return box

    def connection(self):
        connection=MagicMock()
        connection.select.return_value=('OK',[b'101'])
        connection.response.return_value=('UIDVALIDITY',[b'1'])
        payload=('OK',[(b'101 (UID 101 BODY[HEADER.FIELDS (FROM REPLY-TO SUBJECT DATE MESSAGE-ID)] {120}',
            b'From: =?utf-8?q?=C5=BBaneta?= <zaneta@example.com>\r\nTo: inbox@example.com\r\nSubject: =?utf-8?q?Za=C5=BC=C3=B3=C5=82=C4=87?=\r\nDate: Sun, 27 Sep 2026 10:00:00 +0200\r\n\r\n'),b')'])
        connection.uid.side_effect=lambda command,*args: ('OK',[b' '.join(str(i).encode() for i in range(1,102))]) if command=='SEARCH' else payload
        return connection

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_only_headers_read_only_and_pagination(self, connect):
        connection=self.connection();connect.return_value=connection
        result=read_headers(self.box())
        connection.select.assert_called_once_with('"INBOX"',readonly=True)
        self.assertEqual(connection.uid.call_args.args,('FETCH',','.join(map(str,range(52,102))),'(UID BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT DATE MESSAGE-ID)]<0.65536>)'))
        self.assertEqual(result['rows'][0]['subject'],'Zażółć')
        self.assertIn('Żaneta',result['rows'][0]['sender'])
        self.assertEqual(result['next_cursor']['boundary'],52)
        self.assertEqual([c[0] for c in connection.method_calls],['login','select','response','uid','uid','logout'])
        read_headers(self.box(),result['next_cursor'])
        self.assertEqual(connection.uid.call_args.args[1],','.join(map(str,range(2,52))))

    @patch('core.services.mailbox.imaplib.IMAP4')
    def test_starttls_before_credentials(self, connect):
        connection=self.connection();connect.return_value=connection
        read_headers(self.box(security='starttls',port=143))
        self.assertEqual([c[0] for c in connection.method_calls][:2],['starttls','login'])

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_empty_folder_and_logout(self, connect):
        connection=self.connection();connect.return_value=connection
        connection.select.return_value=('OK',[b'0'])
        connection.uid.side_effect=lambda *args: ('OK',[b''])
        self.assertEqual(read_headers(self.box())['rows'],[])
        self.assertEqual(connection.uid.call_count,1);connection.logout.assert_called_once()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_failed_login_hides_server_details_and_logs_out(self, connect):
        import imaplib
        connection=self.connection();connect.return_value=connection
        connection.login.side_effect=imaplib.IMAP4.error('password secret-not-in-html rejected')
        with self.assertRaises(MailboxError) as caught:read_headers(self.box())
        self.assertNotIn('secret-not-in-html',str(caught.exception))
        connection.logout.assert_called_once();connection.select.assert_not_called()

    def test_encrypted_password_and_secret_rotation(self):
        box=self.box()
        self.assertNotIn('secret-not-in-html',box.encrypted_password)
        self.assertEqual(box.get_password(),'secret-not-in-html')
        from django.conf import settings
        original=settings.SECRET_KEY
        with self.settings(SECRET_KEY='rotated-key',SECRET_KEY_FALLBACKS=[original]):
            self.assertEqual(box.get_password(),'secret-not-in-html')


class IntakeAndVacationTests(TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        settings=self.settings(ACTIVITY_SPOOL_DIR=self.tmp.name);settings.enable();self.addCleanup(settings.disable)
        User=get_user_model()
        self.admin=User.objects.create_superuser('admin','admin@example.com','test-password')
        self.user=User.objects.create_user('member',email='member@example.com')
        self.person=Person.objects.create(user=self.user,first_name='Jan',last_name='Testowy',email=self.user.email,is_active=True)
        self.coordinator=User.objects.create_user('coordinator',email='coordinator@example.com')
        self.coordinator_person=Person.objects.create(user=self.coordinator,first_name='Ala',last_name='Testowa',email=self.coordinator.email,is_active=True)
        self.role=Role.objects.get_or_create(name='Koordynator korekty')[0];self.coordinator_person.roles.add(self.role)
        self.book=Anthology.objects.create(title='Otwarta')
        self.client.force_login(self.admin)

    def test_combined_forms_distinct_ids_and_invalid_single_retained(self):
        response=self.client.get(reverse('core:review_create'))
        self.assertContains(response,'Pojedyncze zgłoszenie')
        self.assertContains(response,'Wiele zgłoszeń')
        from html.parser import HTMLParser
        class IDs(HTMLParser):
            def __init__(self):super().__init__();self.ids=[]
            def handle_starttag(self, tag, attrs):
                value=dict(attrs).get('id')
                if value:self.ids.append(value)
        parser=IDs();parser.feed(response.content.decode())
        self.assertEqual(len(parser.ids),len(set(parser.ids)))
        response=self.client.post(reverse('core:review_create'),{'title':'Zachowaj tytuł'})
        self.assertEqual(response.status_code,400)
        self.assertContains(response,'Zachowaj tytuł',status_code=400)
        self.assertContains(response,'Wiele zgłoszeń',status_code=400)
        self.assertEqual(Review.objects.count(),0)

    def test_bulk_preview_then_import(self):
        url=reverse('core:review_bulk_submit')
        payload={'anthology':self.book.pk,'records':'Jan Autor;Opowiadanie;fantasy;1000;;jan@example.com;','import_action':'preview'}
        response=self.client.post(url,payload)
        self.assertEqual(response.status_code,200,response.content[:2000])
        self.assertContains(response,'Pojedyncze zgłoszenie')
        self.assertTrue(response.context['can_import'],response.context['bulk_form'].errors)
        self.assertEqual(Review.objects.count(),0)
        payload.update(import_action='import',preview_token=response.context['preview_token'])
        response=self.client.post(url,payload)
        self.assertEqual(response.status_code,302)
        self.assertEqual(Review.objects.count(),1)

    def test_config_password_required_kept_and_not_rendered(self):
        values={'name':'Skrzynka','host':'imap.example.com','port':993,'security':'ssl','username':'test','folder':'INBOX','is_active':True}
        self.assertFalse(MailboxConnectionForm(values).is_valid())
        form=MailboxConnectionForm(dict(values,password='secret-value'))
        self.assertTrue(form.is_valid(),form.errors);box=form.save()
        form=MailboxConnectionForm(dict(values,password=''),instance=box)
        self.assertTrue(form.is_valid(),form.errors);form.save()
        self.assertEqual(box.get_password(),'secret-value')
        response=self.client.get(reverse('admin:core_mailboxconnection_change',args=[box.pk]))
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'secret-value');self.assertNotContains(response,box.encrypted_password)

    @patch('core.views.mailbox.read_headers')
    def test_mail_preview_access_no_get_network_no_insert_and_escape(self, read):
        box=MailboxConnection.objects.create(name='teksty',host='imap.example.com',username='test',encrypted_password='unused')
        url=reverse('core:review_bulk_import')
        self.assertEqual(self.client.get(url).status_code,200);read.assert_not_called()
        read.return_value={'rows':[{'uid':12,'subject':'<script>alert(1)</script>','sender':'Sender'}],'validity':1,'total':1,'page':1,'pages':1}
        response=self.client.post(url,{'mailbox':box.pk})
        self.assertContains(response,'&lt;script&gt;');self.assertNotContains(response,'<script>alert(1)</script>')
        self.assertEqual(Review.objects.count(),0)
        for user in (self.coordinator,self.user):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url,{'mailbox':box.pk}).status_code,403)
            self.assertEqual(self.client.get(reverse('admin:core_mailboxconnection_changelist')).status_code,302)
        self.assertEqual(read.call_count,1)

    def test_coordinator_can_create_other_person_vacation(self):
        self.client.force_login(self.coordinator)
        url=reverse('core:my_vacations')
        self.assertContains(self.client.get(url),'id="vacation-person"')
        response=self.client.post(url,{'person':self.person.pk,'start_date':timezone.localdate().isoformat(),'until_revoked':'on'})
        self.assertEqual(response.status_code,302)
        self.assertTrue(Vacation.objects.filter(person=self.person).exists())

    def test_member_cannot_create_other_person_vacation(self):
        with self.assertRaises(PermissionDenied):
            create_vacation(user=self.user,person_id=self.coordinator_person.pk,start_date=timezone.localdate(),end_date=None,until_revoked=True)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse('core:my_vacations')),'id="vacation-person"')
