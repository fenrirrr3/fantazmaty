from email.message import EmailMessage
from lxml import html
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, Client, SimpleTestCase
from django.urls import reverse

from core.models import MailboxConnection, Recruitment, RecruitmentMailSource, MailboxDownload
from core.services.mailbox import MailboxError, read_headers
from core.services.mailbox_import import mailbox_key, fetch_messages
from core.views.recruitment_mailbox import SALT


def sample(uid=12, html=False, attachment=False):
    msg = EmailMessage()
    msg['From'] = 'Kandydat <candidate@example.test>'
    msg['Subject'] = f'Redakcja, Korekta audiobooków – zgłoszenie {uid}'
    msg['Date'] = 'Thu, 8 Oct 2026 10:20:00 +0200'
    if html:
        msg.set_content('<html><body><p>Moje zgłoszenie</p><script>alert(1)</script><img src="https://remote.invalid/tracker">Drugi akapit</body></html>', subtype='html')
    else:
        msg.set_content(f'Pełna treść zgłoszenia {uid}.')
    if attachment:
        msg.add_attachment(b'sample-document', maintype='application', subtype='octet-stream', filename='../../próbka.docx')
    return {'uid': uid, 'raw': msg.as_bytes()}


class RecruitmentPreviewJavaScriptTests(SimpleTestCase):
    @skipUnless(shutil.which('node'), 'Test podglądu wymaga Node.js.')
    def test_preview_selection_races_and_safe_text(self):
        script = Path(__file__).with_name('js_tests') / 'recruitment_preview.cjs'
        result = subprocess.run([shutil.which('node'), str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class RecruitmentSamplesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('samples-admin', 'samples@example.test', 'test-only')
        cls.box = MailboxConnection.objects.create(name='rekrutacja', purpose='recruitment', host='imap.example.test', username='samples@example.test', encrypted_password='unused')

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse('core:recruitment_mailbox')
        patcher = patch('core.views.recruitment_mailbox.read_headers', return_value=dict(validity=7,
            rows=[dict(uid=12, sender='candidate@example.test', subject='Redakcja', date='')], total=1, next_cursor=None, previous_cursor=None))
        self.headers = patcher.start(); self.addCleanup(patcher.stop)
        self.client.get(self.url)

    def payload(self, action='add', uids=(12,), **extra):
        token = signing.dumps(dict(user=self.user.pk, mailbox=mailbox_key(self.box), roles=[], show=False,
            kind='selection', validity=7, uids=list(uids)), salt=SALT)
        return {'action': action, 'roles': ['all'], 'selection': token, 'selected': list(uids), **extra}

    def test_all_is_default_and_headers_survive_return_navigation(self):
        self.assertEqual(self.headers.call_args.kwargs['recruitment_roles'], [])
        self.headers.reset_mock()
        response = self.client.get(self.url)
        self.headers.assert_not_called()
        self.assertContains(response, 'Pobieranie próbek')
        self.assertContains(response, 'value="all" selected')
        self.assertContains(response, 'data-mail-preview')
        self.assertContains(response, '50 wiadomości')

    @patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample(html=True)])
    def test_preview_is_plain_text_does_not_save_and_requires_signed_selection(self, fetch):
        response = self.client.post(self.url, self.payload('preview', uid=12))
        self.assertTrue(response['Content-Type'].startswith('text/html'))
        doc = html.fromstring(response.content.decode('utf-8'))
        data = {'body': doc.xpath('string(//*[@data-preview-body])')}
        self.assertIn('Moje zgłoszenie', data['body'])
        self.assertNotIn('<script', data['body']); self.assertNotIn('alert(1)', data['body'])
        self.assertNotIn('tracker', data['body'])
        self.assertFalse(Recruitment.objects.exists()); self.assertFalse(MailboxDownload.objects.exists())
        response = self.client.post(self.url, self.payload('preview', uid=99))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fetch.call_count, 1)
        # The non-JS preview renders the same escaped plain text beside Rola.
        response = self.client.post(self.url, self.payload('preview_html', preview_uid=12))
        self.assertContains(response, 'Moje zgłoszenie')
        self.assertFalse(Recruitment.objects.exists())
        self.assertContains(response, 'data-preview-uid="12"')
        self.assertNotContains(response, '<th>Podgląd</th>')

    def test_preview_fetches_fresh_mail_and_escapes_literal_markup(self):
        raw = EmailMessage()
        raw['Subject'] = 'Tytuł <img src="https://remote.invalid/title">'
        raw.set_content('Nowa treść z serwera <img src="https://remote.invalid/body" onerror="alert(1)">')
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[{'uid': 12, 'raw': raw.as_bytes()}]) as fetch:
            response = self.client.post(self.url, self.payload('preview', uid=12))
        fetch.assert_called_once_with(self.box, 7, {12}, raw_messages=True, max_messages=50)
        self.assertContains(response, 'Nowa treść z serwera')
        self.assertContains(response, '&lt;img')
        doc = html.fromstring(response.content.decode('utf-8'))
        self.assertFalse(doc.xpath('//img|//script'))
        self.assertFalse(Recruitment.objects.exists())

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_add_50_messages_then_retry_does_not_duplicate_and_does_not_mark_downloaded(self, fetch):
        fetch.return_value = [sample(uid) for uid in range(1, 51)]
        response = self.client.post(self.url, self.payload(uids=range(1, 51)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fetch.call_args.kwargs['max_messages'], 50)
        self.assertEqual(Recruitment.objects.count(), 50)
        self.assertEqual(RecruitmentMailSource.objects.count(), 50)
        self.assertFalse(MailboxDownload.objects.exists())
        self.client.post(self.url, self.payload(uids=range(1, 51)))
        self.assertEqual(Recruitment.objects.count(), 50)
        record = Recruitment.objects.first()
        self.assertIsNone(record.accepted); self.assertFalse(record.notified)
        self.assertEqual(record.mail_roles, ['editors', 'audio_proofreaders'])
        self.assertEqual(record.first_name, '')
        fetch.reset_mock()
        self.assertEqual(self.client.post(self.url, self.payload(uids=range(1, 52))).status_code, 400)
        fetch.assert_not_called()

    @patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample(attachment=True)])
    def test_download_only_attachments_registers_and_preserves_filename_safely(self, fetch):
        response = self.client.post(self.url, self.payload('download'))
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(b''.join(response.streaming_content))) as archive:
            self.assertEqual(archive.namelist(), ['wiadomosc-12/01-próbka.docx'])
            self.assertEqual(archive.read(archive.namelist()[0]), b'sample-document')
        self.assertEqual(Recruitment.objects.count(), 1)
        self.assertIsNotNone(RecruitmentMailSource.objects.get().downloaded_at)
        self.assertTrue(MailboxDownload.objects.exists())
        response = self.client.get(reverse('core:recruitment_list'))
        self.assertContains(response, 'candidate@example.test')
        self.assertContains(response, 'zgłoszenie 12')

    @patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample()])
    def test_bulk_decision_and_notified_are_independent_and_retry_preserves_both(self, fetch):
        self.assertEqual(self.client.post(self.url, self.payload('bulk', decision='yes')).status_code, 302)
        record = Recruitment.objects.get(); self.assertTrue(record.accepted); self.assertFalse(record.notified)
        self.client.post(self.url, self.payload('bulk', notified='yes'))
        record.refresh_from_db(); self.assertTrue(record.accepted); self.assertTrue(record.notified)
        first_date = record.notified_at
        self.client.post(self.url, self.payload('add'))
        record.refresh_from_db(); self.assertTrue(record.accepted); self.assertEqual(record.notified_at, first_date)
        self.client.post(self.url, self.payload('bulk', decision='no'))
        record.refresh_from_db(); self.assertFalse(record.accepted); self.assertTrue(record.notified)
        self.client.post(self.url, self.payload('bulk', decision='clear', notified='no'))
        record.refresh_from_db(); self.assertIsNone(record.accepted); self.assertFalse(record.notified); self.assertIsNone(record.notified_at)
        self.assertEqual(Recruitment.objects.count(), 1)
        response = self.client.get(self.url)
        self.assertContains(response, 'Bez decyzji'); self.assertContains(response, 'W bazie')

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_changed_mail_rolls_back_whole_batch_and_invalid_bulk_never_fetches(self, fetch):
        fetch.return_value = [sample()]
        self.client.post(self.url, self.payload())
        original = Recruitment.objects.get().mail_body
        changed = sample(); changed['raw'] += b'Changed content'
        fetch.return_value = [sample(13), changed]
        response = self.client.post(self.url, self.payload(uids=[12, 13]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Recruitment.objects.count(), 1); self.assertEqual(RecruitmentMailSource.objects.count(), 1)
        self.assertEqual(Recruitment.objects.get().mail_body, original)
        fetch.reset_mock()
        self.assertEqual(self.client.post(self.url, self.payload('bulk', decision='invalid')).status_code, 400)
        fetch.assert_not_called()

    def test_csrf_required_for_writes_and_preview(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.user)
        for action in ('add', 'download', 'bulk', 'preview'):
            self.assertEqual(client.post(self.url, self.payload(action, uid=12)).status_code, 403)

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_all_search_has_no_subject_filter_and_preserves_complete_subject(self, imap):
        client = imap.return_value; client.select.return_value = ('OK', [b'1']); client.response.return_value = ('UIDVALIDITY', [b'7'])
        client.uid.side_effect = [('OK', [b'12']), ('OK', [(b'1 (UID 12 BODY[])', sample()['raw'])])]
        with patch.object(self.box, 'get_password', return_value='test'):
            result = read_headers(self.box, recruitment_roles=[])
        self.assertEqual(client.uid.call_args_list[0].args, ('SEARCH', None, 'UID', '1:*'))
        self.assertIn('Korekta audiobooków – zgłoszenie', result['rows'][0]['subject'])

    @patch('core.services.mailbox_import.imaplib.IMAP4_SSL')
    def test_raw_service_accepts_50_but_submission_import_retains_20(self, imap):
        raw = sample()['raw']; client = imap.return_value
        client.select.return_value = ('OK', [b'50']); client.response.return_value = ('UIDVALIDITY', [b'7'])
        def fetch(command, uid, fields):
            if 'RFC822.SIZE' in fields:
                return ('OK', [f'1 (UID {uid} RFC822.SIZE {len(raw)})'.encode()])
            return ('OK', [(f'1 (UID {uid} BODY[])'.encode(), raw)])
        client.uid.side_effect = fetch
        with patch.object(self.box, 'get_password', return_value='test'):
            self.assertEqual(len(fetch_messages(self.box, 7, range(1, 51), raw_messages=True, max_messages=50)), 50)
            with self.assertRaises(MailboxError):
                fetch_messages(self.box, 7, range(1, 22))
