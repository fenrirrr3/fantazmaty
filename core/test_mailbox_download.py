from io import BytesIO
from email.message import EmailMessage
from unittest.mock import patch, MagicMock
from zipfile import ZipFile
from django.test import TestCase, SimpleTestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core import signing
from docx import Document
from core.models import MailboxConnection, MailboxDownload
from core.services.mailbox import polish_date, story_title, read_headers, MailboxError
from core.services.mailbox_import import parse_message, package_messages, fetch_messages, mailbox_key, default_mailbox
from texts.models import Anthology, Review


def mail(uid=12, line='Julia Moskalik;Potworna Przystan;dark fantasy;15204;girl@example.com;885711216;Na pokład, psubraty;IGNORUJ;TEŻ'):
    msg=EmailMessage(); msg['From']='other@example.com';msg['Reply-To']='reply@example.com'
    msg['Subject']='Julia Moskalik – Potworna Przystan';msg['Date']='Tue, 12 May 2026 10:30:00 +0200'
    msg.set_content(line+'\nTa treść nie może trafić do zgłoszenia.')
    doc=Document();doc.add_paragraph('Koniec. następne zdanie.');stream=BytesIO();doc.save(stream)
    msg.add_attachment(stream.getvalue(),maintype='application',subtype='vnd.openxmlformats-officedocument.wordprocessingml.document',filename='tekst.docx')
    return msg.as_bytes()


class MailParsingTests(SimpleTestCase):
    def test_fields_ignore_trailing_and_body(self):
        parsed=parse_message(12,mail())
        self.assertEqual(parsed['record'],'Julia Moskalik;Potworna Przystan;dark fantasy;15204;;girl@example.com;885711216')
        self.assertEqual(parsed['folder'],'Potworna Przystan')
        self.assertNotIn('IGNORUJ',str(parsed['record']))

    def test_invalid_mail_and_multiple_records(self):
        for line in ('Brak danych', 'A B;T;g;2;a@b.pl;;N\nC D;T;g;2;c@d.pl;;N'):
            with self.assertRaises(MailboxError):parse_message(1,mail(line=line))

    def test_polish_date_and_title(self):
        self.assertEqual(polish_date('Tue, 12 May 2026 10:30:00 +0200'),'12 maja 2026, 10:30')
        self.assertEqual(polish_date('bad'),'–')
        self.assertEqual(story_title('Autor – Tytuł – część druga'),'Tytuł – część druga')

    def test_original_and_converted_zip(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        row=parse_message(12,mail()); row['folder']='../CON'
        with TemporaryDirectory() as tmp, self.settings(DOCUMENT_CONVERSION_DIR=Path(tmp)):
            for clean,convert in ((False,False),(True,False),(False,True),(True,True)):
                with self.subTest(clean=clean,convert=convert), package_messages([row],clean,convert) as out, ZipFile(out) as z:
                    self.assertEqual(len(z.namelist()),3 if convert else 1)
                    self.assertTrue(all('..' not in p.split('/') for p in z.namelist()))
                    doc=Document(BytesIO(z.read(next(p for p in z.namelist() if p.endswith('.docx')))))
                    self.assertEqual(doc.paragraphs[0].text,'Koniec. Następne zdanie.' if clean else 'Koniec. następne zdanie.')

    @patch('core.services.mailbox_import.imaplib.IMAP4_SSL')
    def test_fetch_readonly_peek_and_uidvalidity(self, imap):
        raw=mail(); client=imap.return_value
        client.select.return_value=('OK',[b'1']);client.response.return_value=('UIDVALIDITY',[b'7'])
        client.uid.side_effect=[('OK',[f'1 (UID 12 RFC822.SIZE {len(raw)})'.encode()]),('OK',[(b'1 (UID 12 BODY[] {})',raw)])]
        box=MailboxConnection(host='imap.example.com',username='teksty@example.com');box.set_password('test')
        result=fetch_messages(box,7,[12]);self.assertEqual(result[0]['uid'],12)
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertIn('BODY.PEEK[]',client.uid.call_args.args[2]);client.logout.assert_called_once()
        self.assertFalse(client.store.called);self.assertFalse(client.expunge.called)
        with self.assertRaises(MailboxError):fetch_messages(box,8,[12])


class MailDownloadTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_superuser('admin','admin@example.com','password')
        self.client.force_login(self.user)
        self.box=MailboxConnection.objects.create(name='teksty',host='imap.example.com',username='teksty@example.com',encrypted_password='unused')
        self.book=Anthology.objects.create(title='Na pokład, psubraty',status=Anthology.Status.IN_PREPARATION)
        self.url=reverse('core:review_bulk_import')
        self.selection=signing.dumps({'user':self.user.pk,'mailbox':mailbox_key(self.box),'validity':7,'uids':[12]},salt='mailbox-selection')

    def payload(self):
        return {'action':'download','selection':self.selection,'uids':['12']}

    @patch('core.views.mailbox.fetch_messages')
    def test_preview_commit_retry(self, fetch):
        raw = mail()
        fetch.side_effect=lambda *args: [parse_message(12,raw)]
        payload=self.payload();response=self.client.post(self.url,payload)
        self.assertEqual(response.status_code,200,response.content[:500]);self.assertEqual(Review.objects.count(),0)
        payload.update(action='confirm',preview=response.context['preview_token'],approve='on')
        response=self.client.post(self.url,payload)
        self.assertEqual(response.status_code,200,response.context.get('error') if not response.streaming else '')
        data=b''.join(response.streaming_content);response.close()
        with ZipFile(BytesIO(data)) as z:self.assertEqual(len(z.namelist()),1)
        self.assertEqual(Review.objects.count(),1);self.assertEqual(MailboxDownload.objects.count(),1)
        row=Review.objects.get();self.assertEqual(row.length,15204);self.assertEqual(row.content_warnings,'')
        response=self.client.post(self.url,payload); self.assertTrue(response.streaming);response.close()
        self.assertEqual(Review.objects.count(),1)

    @patch('core.views.mailbox.fetch_messages')
    def test_conversion_failure_no_records(self, fetch):
        fetch.return_value=[parse_message(12,mail())]
        payload=self.payload();preview=self.client.post(self.url,payload).context['preview_token']
        payload.update(action='confirm',preview=preview,approve='on')
        with patch('core.views.mailbox.package_messages',side_effect=MailboxError('Niepoprawny plik')):
            self.assertEqual(self.client.post(self.url,payload).status_code,400)
        self.assertFalse(Review.objects.exists());self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_selection_tampering(self, fetch):
        data=self.payload();data['uids']=['13']
        self.assertEqual(self.client.post(self.url,data).status_code,400);fetch.assert_not_called()

    @patch('core.views.mailbox.read_headers')
    def test_default_box_and_filtered_headers(self, read):
        MailboxDownload.objects.create(mailbox_key=mailbox_key(self.box),uid_validity=7,uid=12)
        read.return_value={'rows':[],'validity':7,'total':0}
        response=self.client.get(self.url);read.assert_not_called()
        self.assertNotContains(response,'name="mailbox"');self.assertContains(response,'name="clean" checked')
        self.client.post(self.url,{'action':'headers'})
        self.assertEqual(list(read.call_args.kwargs['excluded'](7)),[12])
        self.client.post(self.url,{'action':'headers','show_downloaded':'on'})
        self.assertIsNone(read.call_args.kwargs['excluded'])

    def test_ambiguous_box(self):
        MailboxConnection.objects.create(name='TEKSTY',host='imap.example.com',username='other',encrypted_password='unused')
        with self.assertRaises(MailboxError):default_mailbox()

    @patch('core.views.mailbox.fetch_messages')
    def test_changed_content_requires_new_preview(self, fetch):
        raw=mail();fetch.return_value=[parse_message(12,raw)]
        payload=self.payload();preview=self.client.post(self.url,payload).context['preview_token']
        fetch.return_value=[parse_message(12,mail(line='Julia Moskalik;Zmieniony;fantasy;100;girl@example.com;;Na pokład, psubraty'))]
        payload.update(action='confirm',preview=preview,approve='on')
        self.assertEqual(self.client.post(self.url,payload).status_code,400)
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_non_preparation_anthology_rejected(self, fetch):
        fetch.return_value=[parse_message(12,mail())]
        self.book.status='ready';self.book.save()
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,400)
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_duplicate_warning_requires_confirmation_and_token(self, fetch):
        Review.objects.create(anthology=self.book,title='Potworna Przystan',author_first_name='JULIA',
            author_last_name='MOSKALIK',email='girl@example.com',length=15204,genre='dark fantasy')
        raw=mail();fetch.side_effect=lambda *args:[parse_message(12,raw)]
        payload=self.payload();response=self.client.post(self.url,payload)
        self.assertTrue(response.context['forms'][0].import_warnings)
        payload.update(action='confirm',preview=response.context['preview_token'])
        response=self.client.post(self.url,payload)
        self.assertEqual(response.status_code,400);self.assertEqual(Review.objects.count(),1)
        payload.update(approve='on',preview=response.context['preview_token'])
        response=self.client.post(self.url,payload);self.assertTrue(response.streaming);response.close()
        self.assertEqual(Review.objects.count(),2)

    def test_non_superuser_forbidden(self):
        user=get_user_model().objects.create_user('member','member@example.com','password')
        self.client.force_login(user)
        with patch('core.views.mailbox.fetch_messages') as fetch:
            self.assertEqual(self.client.post(self.url,self.payload()).status_code,403)
            fetch.assert_not_called()
