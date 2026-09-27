from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO
from unittest.mock import patch, MagicMock
from django.test import TestCase, SimpleTestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone
from authors.models import Author
from texts.models import Text, Anthology, Review
from texts.admin import TextAdminForm
from texts.services import preview_author_matches
from workflow.models import WorkflowStage as S
from core.activity_spool import enqueue_activity
from core.models import UserActivity, MailboxConnection
from core.intake_forms import SingleReviewForm
from core.services.mailbox import read_headers, encode_folder, MailboxError


class SixFixesTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        settings=self.settings(ACTIVITY_SPOOL_DIR=self.tmp.name);settings.enable();self.addCleanup(settings.disable)
        self.user=get_user_model().objects.create_superuser('admin','admin@example.com','unused')
        self.client.force_login(self.user)
        self.book=Anthology.objects.create(title='Open')
        self.ready=Anthology.objects.create(title='Ready',status='ready')
        self.author=Author.objects.create(first_name='Jan',last_name='Autor',email='jan@example.com')
        self.text=Text.objects.create(title='Text',anthology=self.book,length=1000)
        self.text.authors.add(self.author)

    def test_active_move_blocked_in_form_and_save(self):
        S.objects.create(text=self.text,stage_type='ready_for_editing')
        form=TextAdminForm({'title':'Text','length':1000,'authors':[self.author.pk],'anthology':self.ready.pk},instance=self.text)
        self.assertFalse(form.is_valid());self.assertIn('anthology',form.errors)
        self.text.anthology=self.ready
        with self.assertRaises(ValidationError):self.text.save()
        self.text.refresh_from_db();self.assertEqual(self.text.anthology_id,self.book.pk)

    def test_finished_and_withdrawn_moves_allowed(self):
        stage=S.objects.create(text=self.text,stage_type='ready')
        self.text.anthology=self.ready;self.text.full_clean();self.text.save()
        self.text.anthology=self.book;self.text.save()
        stage.stage_type='withdrawn';stage.save()
        self.text.anthology=self.ready;self.text.full_clean();self.text.save()

    def test_save_rechecks_after_form_validation(self):
        other=Anthology.objects.create(title='Other')
        form=TextAdminForm({'title':'Text','length':1000,'authors':[self.author.pk],'anthology':other.pk},instance=self.text)
        self.assertTrue(form.is_valid(),form.errors)
        other.status='ready';other.save()
        with self.assertRaises(ValidationError):form.save()
        self.text.refresh_from_db();self.assertEqual(self.text.anthology_id,self.book.pk)

    def test_closing_anthology_during_single_review_validation(self):
        original=SingleReviewForm.save
        def changed(form,*args,**kwargs):
            Anthology.objects.filter(pk=self.book.pk).update(status='ready')
            return original(form,*args,**kwargs)
        with patch.object(SingleReviewForm,'save',changed):
            response=self.client.post(reverse('core:review_create'),{'author_first_name':'Anna','author_last_name':'Nowa','email':'anna@example.com','genre':'fantasy','title':'Review','length':1000,'anthology':self.book.pk})
        self.assertEqual(response.status_code,400)
        self.assertContains(response,'nabór nie jest już dostępny',status_code=400)
        self.assertFalse(Review.objects.exists())

    def record(self):
        enqueue_activity(user_id=self.user.pk,actor='Test',method='GET',action='Pulpit',target='',path='/',status_code=200)

    def test_bad_json_quarantined_good_entries_imported(self):
        bad=Path(self.tmp.name)/('0'*32+'.json');bad.write_text('{bad')
        self.record();self.record()
        call_command('flush_activity',stdout=StringIO(),stderr=StringIO())
        self.assertEqual(UserActivity.objects.count(),2)
        self.assertTrue((Path(self.tmp.name)/'quarantine'/bad.name).exists())
        self.assertFalse(list(Path(self.tmp.name).glob('*.json')))
        call_command('flush_activity',stdout=StringIO(),stderr=StringIO())
        self.assertEqual(UserActivity.objects.count(),2)

    def test_database_error_keeps_good_record_for_retry(self):
        from django.core.management import CommandError
        self.record()
        with patch.object(UserActivity.objects,'get_or_create',side_effect=OperationalError('unavailable')):
            with self.assertRaises(CommandError):call_command('flush_activity',stdout=StringIO(),stderr=StringIO())
        self.assertEqual(len(list(Path(self.tmp.name).glob('*.json'))),1)
        self.assertFalse((Path(self.tmp.name)/'quarantine').exists())
        call_command('flush_activity',stdout=StringIO(),stderr=StringIO())
        self.assertEqual(UserActivity.objects.count(),1)

    def test_preview_batched_queries_and_case_insensitive_email(self):
        with self.assertNumQueries(1):
            matches=preview_author_matches(['JAN@EXAMPLE.COM','unknown@example.com','jan@example.com'])
        self.assertEqual(matches['jan@example.com'],[self.author])
        self.assertEqual(matches['unknown@example.com'],[])
        with self.assertNumQueries(5):preview_author_matches(f'test{i}@example.com' for i in range(500))

    @patch('core.views.mailbox.read_headers')
    def test_cursor_bound_to_user_and_mailbox_settings(self,read):
        box=MailboxConnection.objects.create(name='teksty',host='imap.example.com',username='login',encrypted_password='unused')
        cursor={'anchor':10,'boundary':5,'direction':'older','validity':1}
        read.return_value={'validity':1,'rows':[],'total':10,'next_cursor':cursor,'previous_cursor':None}
        url=reverse('core:review_bulk_import')
        response=self.client.post(url,{'mailbox':box.pk})
        token=response.context['result']['next_cursor']
        read.return_value={'validity':1,'rows':[],'total':1}
        self.assertEqual(self.client.post(url,{'mailbox':box.pk,'cursor':token}).status_code,200)
        self.assertEqual(read.call_args.args[1],cursor)
        box.folder='Other';box.save()
        self.assertEqual(self.client.post(url,{'mailbox':box.pk,'cursor':token}).status_code,400)
        self.assertEqual(read.call_count,2)


class MailboxCursorTests(SimpleTestCase):
    def test_folder_encoding(self):
        self.assertEqual(encode_folder('Kosz & Wysłane'),'Kosz &- Wys&AUI-ane')
        self.assertEqual(encode_folder('~peter/mail/台北/日本語'),'~peter/mail/&U,BTFw-/&ZeVnLIqe-')
        self.assertEqual(encode_folder('INBOX'),'INBOX')

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_new_and_deleted_messages_do_not_shift_cursor(self,connect):
        box=MailboxConnection(host='imap.example.com',username='login',folder='Wysłane & odebrane');box.set_password('secret')
        client=MagicMock();connect.return_value=client
        client.select.return_value=('OK',[b'101']);client.response.return_value=('UIDVALIDITY',[b'9'])
        ids=set(range(1,102))
        def uid(command,*args):
            if command=='SEARCH':return 'OK',[b' '.join(str(i).encode() for i in sorted(ids))]
            return 'OK',[(f'1 (UID {i} BODY[] {{20}}'.encode(),b'Subject: Test\r\n\r\n') for i in map(int,args[0].split(',')) if i in ids]
        client.uid.side_effect=uid
        first=read_headers(box)
        self.assertEqual([r['uid'] for r in first['rows']],list(range(101,51,-1)))
        ids.update([102,103]);ids.remove(90);ids.remove(30)
        second=read_headers(box,first['next_cursor'])
        self.assertEqual([r['uid'] for r in second['rows']],[i for i in range(51,0,-1) if i!=30])
        previous=read_headers(box,second['previous_cursor'])
        self.assertNotIn(103,[r['uid'] for r in previous['rows']])
        self.assertNotIn(102,[r['uid'] for r in previous['rows']])
        self.assertEqual(client.select.call_args.kwargs,{'readonly':True})
        self.assertIn('&AUI-',client.select.call_args.args[0])
        self.assertTrue(all(call.args[0] in ('SEARCH','FETCH') for call in client.uid.call_args_list))
        client.response.return_value=('UIDVALIDITY',[b'10'])
        with self.assertRaises(MailboxError):read_headers(box,first['next_cursor'])
