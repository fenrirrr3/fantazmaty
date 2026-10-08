from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse

from core.models import MailboxConnection, MailboxDownload, Recruitment
from core.recruitment_roles import ROLE_CHOICES
from core.services.mailbox import read_headers, MailboxError
from core.services.mailbox_import import default_mailbox, fetch_messages, mailbox_key
from core.services.mailbox_email_copy import sender_emails
from core.views.recruitment_mailbox import SALT


class RecruitmentMailboxTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('recruitment-admin', 'admin@example.test', 'test-only')
        cls.member = get_user_model().objects.create_user('ordinary', 'ordinary@example.test', 'test-only')
        cls.submissions = MailboxConnection.objects.create(name='teksty', host='imap.example.test', username='teksty@example.test', encrypted_password='unused')
        cls.box = MailboxConnection.objects.create(name='rekrutacja', purpose='recruitment', host='imap.example.test', username='rekrutacja@example.test', encrypted_password='unused')

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse('core:recruitment_mailbox')
        patcher = patch('core.views.recruitment_mailbox.read_headers', return_value=dict(validity=7, rows=[], total=0, next_cursor=None, previous_cursor=None))
        self.header_reader = patcher.start()
        self.addCleanup(patcher.stop)

    def token(self, **changes):
        data = {'user': self.user.pk, 'mailbox': mailbox_key(self.box), 'roles': ['editors', 'reviewers'],
                'show': False, 'kind': 'selection', 'validity': 7, 'uids': [12, 13]}
        data.update(changes)
        return signing.dumps(data, salt=SALT)

    def test_page_roles_order_admin_and_no_conversion_options(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['form'].fields['roles'].choices), [('all', 'Wszystkie'), *ROLE_CHOICES])
        for field in ('clean', 'rebuild', 'convert', 'subject_filter'):
            self.assertNotContains(response, f'name="{field}"')
        self.assertContains(response, 'multiple')
        self.assertTrue(admin.site.is_registered(Recruitment))
        response = self.client.get(reverse('admin:index'))
        self.assertContains(response, 'Skrzynka rekrutacyjna')
        self.assertContains(response, reverse('admin:core_recruitment_changelist'))
        response = self.client.get(reverse('admin:core_mailboxconnection_change', args=[self.box.pk]))
        self.assertContains(response, 'name="purpose"')
        self.assertNotContains(response, 'name="recruitment_subjects"')

    @patch('core.views.recruitment_mailbox.read_headers')
    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_non_superuser_cannot_read_download_or_copy_mail(self, fetch, read):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        for action in ('headers', 'download', 'copy_emails', 'preview', 'add', 'bulk'):
            self.assertEqual(self.client.post(self.url, {'action': action, 'roles': ['editors']}).status_code, 403)
        fetch.assert_not_called(); read.assert_not_called()

    def test_separate_mailbox_selection_and_receipt_keys(self):
        self.box.name = 'teksty'; self.box.save()
        self.assertEqual(default_mailbox(), self.submissions)
        recruitment_key = mailbox_key(self.box)
        self.box.purpose = 'submissions'
        self.assertNotEqual(recruitment_key, mailbox_key(self.box))

    @patch('core.views.recruitment_mailbox.read_headers')
    def test_multiple_filters_signed_cursor_and_selection(self, read):
        cursor = {'anchor': 100, 'boundary': 51, 'direction': 'older', 'validity': 7}
        read.return_value = {'rows': [{'uid': 12, 'sender': 'a@example.test', 'subject': 'Redakcja, Recenzje', 'date': ''}],
                            'total': 60, 'validity': 7, 'next_cursor': cursor, 'previous_cursor': None}
        response = self.client.post(self.url, {'roles': ['reviewers', 'editors'], 'action': 'headers'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read.call_args.kwargs['recruitment_roles'], ['editors', 'reviewers'])
        token = response.context['next_cursor']
        response = self.client.post(self.url, {'roles': ['editors', 'reviewers'], 'cursor': token}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read.call_args.args[1], cursor)
        read.reset_mock()
        response = self.client.post(self.url, {'roles': ['editors'], 'cursor': token})
        self.assertEqual(response.status_code, 400); read.assert_not_called()

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_download_without_attachments_still_registers_message(self, fetch):
        raw = b'From: candidate@example.test\r\nSubject: Redakcja\r\n\r\nPlain text, no attachments.'
        fetch.return_value = [{'uid': 12, 'raw': raw}]
        response = self.client.post(self.url, {'roles': ['editors', 'reviewers'], 'action': 'download', 'selection': self.token(), 'selected': ['12']})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response.streaming)
        self.assertNotIn('Content-Disposition', response)
        self.assertTrue(fetch.call_args.kwargs['raw_messages'])
        self.assertEqual(Recruitment.objects.count(), 1)
        self.assertIsNone(Recruitment.objects.get().accepted)
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_tampered_selection_and_changed_filters_are_rejected(self, fetch):
        for token, selected, roles in [(self.token(user=self.member.pk), ['12'], ['editors', 'reviewers']),
                                       (self.token(), ['99'], ['editors', 'reviewers']),
                                       (self.token(), ['12'], ['reviewers']),
                                       (self.token(), [], ['editors', 'reviewers'])]:
            response = self.client.post(self.url, {'action': 'download', 'roles': roles, 'selected': selected, 'selection': token})
            self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()

    @patch('core.services.mailbox_import.imaplib.IMAP4_SSL')
    def test_raw_fetch_accepts_mail_without_attachments_and_never_modifies_imap(self, imap):
        msg = EmailMessage(); msg['From'] = 'person@example.test'; msg.set_content('Zgłaszam się.')
        raw = msg.as_bytes(); client = imap.return_value
        client.select.return_value = ('OK', [b'1']); client.response.return_value = ('UIDVALIDITY', [b'7'])
        client.uid.side_effect = [('OK', [f'1 (UID 12 RFC822.SIZE {len(raw)})'.encode()]), ('OK', [(b'1 (UID 12 BODY[])', raw)])]
        self.box.set_password('test-only')
        self.assertEqual(fetch_messages(self.box, 7, [12], raw_messages=True), [{'uid': 12, 'raw': raw}])
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertIn('BODY.PEEK[]', client.uid.call_args.args[2])
        client.store.assert_not_called(); client.expunge.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_role_search_unions_results_and_keeps_full_subject(self, imap):
        client = imap.return_value
        client.select.return_value = ('OK', [b'3']); client.response.return_value = ('UIDVALIDITY', [b'7'])
        client.uid.side_effect = [('OK', [b'12 13']), ('OK', [b'13 14']),
            ('OK', [(b'1 (UID 13 BODY[])', 'From: a@example.test\r\nSubject: Redakcja – Recenzje\r\n'.encode('utf-8'))])]
        self.box.set_password('test-only')
        result = read_headers(self.box, recruitment_roles=['editors', 'reviewers'])
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['rows'][0]['subject'], 'Redakcja – Recenzje')
        self.assertEqual(client.uid.call_args.args[1], '12,13,14')
        client.store.assert_not_called()

    @patch('core.services.mailbox_email_copy.read_headers')
    def test_copy_mail_addresses_traverses_pages_without_receipts(self, read):
        cursor = {'anchor': 100, 'boundary': 51, 'direction': 'older', 'validity': 7}
        read.side_effect = [dict(validity=7, rows=[{'sender': 'A <A@example.test>'}], next_cursor=cursor),
                            dict(validity=7, rows=[{'sender': 'a@example.test, b@example.test'}], next_cursor=None)]
        self.assertEqual(sender_emails(self.box, 7, recruitment_roles=['editors']), ['A@example.test', 'b@example.test'])
        self.assertEqual(read.call_args.args[1], cursor)
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.services.mailbox_email_copy.sender_emails', return_value=['a@example.test'])
    def test_copy_endpoint_uses_signed_recruitment_filters(self, copy):
        response = self.client.post(self.url, {'action': 'copy_emails', 'roles': ['editors', 'reviewers'], 'selection': self.token()})
        self.assertEqual(response.json(), {'emails': ['a@example.test']})
        self.assertEqual(copy.call_args.kwargs['recruitment_roles'], ['editors', 'reviewers'])
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.services.mailbox_email_copy.sender_emails', return_value=['b@example.test'])
    @patch('core.views.mailbox.read_headers')
    def test_submission_mail_copy_keeps_show_downloaded_filter(self, read, copy):
        read.return_value = dict(validity=7, rows=[dict(uid=12, sender='b@example.test', subject='Tytuł', date='')],
                                 total=1, next_cursor=None, previous_cursor=None)
        url = reverse('core:review_bulk_import')
        response = self.client.post(url, {'action': 'headers', 'show_downloaded': 'on'}, follow=True)
        token = response.context['selection']
        response = self.client.post(url, {'action': 'copy_emails', 'show_downloaded': 'on', 'selection': token})
        self.assertEqual(response.json(), {'emails': ['b@example.test']})
        self.assertIsNone(copy.call_args.kwargs['excluded'])
        self.assertEqual(copy.call_args.kwargs['subject_filter'], '')
        self.assertFalse(MailboxDownload.objects.exists())
