"""Zgody na newsletter: łączenie zgód, lista dla superużytkownika i odzyskiwanie zgód ze starych wiadomości skrzynki (tylko do odczytu, z wznawianiem)."""
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from core.admin_newsletter_recovery import extract_consent, recover_batch
from core.models import MailboxConnection, NewsletterConsent
from core.pagination import paginate_items
from core.services.mailbox import MailboxError
from core.services.mailbox_body import fetch_text_body
from core.services.newsletters import record_consents
from fantazmaty.testing import create_member, create_superuser


def message(text):
    mail = EmailMessage()
    mail.set_content(text)
    return mail.as_bytes()


class ConsentExtractionTests(SimpleTestCase):
    def test_old_and_new_explicit_consents(self):
        for row in [
            'Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;premierach, naborach',
            'Jan Nowak;Tytuł;fantasy;;1200;jan@example.com;123;premierach, naborach;Wiadomość',
        ]:
            email, flags = extract_consent(message(row))
            self.assertEqual(email, 'jan@example.com')
            self.assertTrue(all(flags.values()))
        self.assertIsNone(extract_consent(message('Napiszę o premierach i naborach: jan@example.com')))
        row = 'Jan Nowak;Tytuł;fantasy;;1200;jan@example.com;123;;Piszę o premierach, naborach'
        self.assertFalse(any(extract_consent(message(row))[1].values()))
        self.assertIsNone(extract_consent(message(row + '\n' + row)))

    def test_consents_before_message_label(self):
        """Słowa „premierach/naborach” w treści wiadomości po etykiecie nie są brane za zgodę."""
        for value in ['premierach', 'naborach', 'premierach, naborach']:
            for boundary in [' Treść wiadomości: ', '\nTreść wiadomości:\n', ';Treść wiadomości: ']:
                raw = message('Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;' + value + boundary
                              + 'Proszę przeczytać tekst')
                email, flags = extract_consent(raw)
                self.assertEqual(email, 'jan@example.com')
                self.assertEqual(flags, {'premieres': 'premierach' in value, 'recruitment': 'naborach' in value})
        raw = message('Jan Nowak;Tytuł;fantasy;;1200;jan@example.com;123;naborach\nTreść wiadomości: premierach')
        self.assertEqual(extract_consent(raw)[1], {'premieres': False, 'recruitment': True})

    def test_recovery_fetches_body_without_large_attachment(self):
        body = b'Jan Autor;Tytul;fantasy;1000;jan@example.test;123;Nabor;premierach'
        client = MagicMock()
        client.uid.side_effect = [
            ('OK', [f'1 (UID 12 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" {len(body)} 1 NIL NIL NIL)'
                    '("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" 90000000 NIL ("ATTACHMENT" NIL)) "MIXED"))'.encode()]),
            ('OK', [(b'1 (UID 12 BODY[1] {100}', body)]),
        ]
        self.assertIn(body, fetch_text_body(client, 12))
        self.assertEqual(client.uid.call_args.args[-1], '(UID BODY.PEEK[1]<0.1048577>)')


class ConsentRecoveryTests(TestCase):
    def test_readonly_peek_idempotent_snapshot(self):
        config = SimpleNamespace(purpose=MailboxConnection.Purpose.SUBMISSIONS, pk=1, host='example.com', port=993,
                                 security='ssl', username='teksty@example.com', folder='INBOX',
                                 get_password=lambda: 'secret')
        raw = message('Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;premierach')
        body = BytesParser().parsebytes(raw).get_payload(decode=True)
        client = MagicMock()
        client.select.return_value = ('OK', [b'1'])
        client.response.return_value = ('UIDVALIDITY', [b'10'])

        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [b'1']
            if args[-1] == '(UID BODYSTRUCTURE)':
                return 'OK', [f'1 (UID 1 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" {len(body)} 1))'.encode()]
            return 'OK', [(b'1 (UID 1 BODY[1] {123}', body), b')']

        client.uid.side_effect = uid
        with patch('core.admin_newsletter_recovery.imaplib.IMAP4_SSL', return_value=client):
            state = recover_batch(config)
            recover_batch(config)
        self.assertTrue(state['done'])
        self.assertEqual(state['matched'], 1)
        self.assertEqual(NewsletterConsent.objects.count(), 1)
        client.select.assert_called_with('"INBOX"', readonly=True)
        self.assertTrue(any('BODY.PEEK' in str(c) for c in client.uid.call_args_list))
        self.assertFalse(any(c.args[0] in ('STORE', 'EXPUNGE') for c in client.uid.call_args_list))
        client.response.return_value = ('UIDVALIDITY', [b'11'])
        with patch('core.admin_newsletter_recovery.imaplib.IMAP4_SSL', return_value=client):
            with self.assertRaises(MailboxError):
                recover_batch(config, state)

    def test_admin_only_no_get_write_and_csrf(self):
        admin = create_superuser('admin', 'admin@example.com', 'pass')
        user = get_user_model().objects.create_user('staff', is_staff=True)
        url = reverse('admin:core_mailbox_recover_newsletters')
        self.client.force_login(user)
        self.assertIn(self.client.get(url).status_code, (302, 403))
        self.client.force_login(admin)
        with patch('core.admin_newsletter_recovery.recover_batch') as action:
            self.assertEqual(self.client.get(url).status_code, 200)
            action.assert_not_called()
        client = Client(enforce_csrf_checks=True)
        client.force_login(admin)
        self.assertEqual(client.post(url).status_code, 403)

    def test_progress_survives_get_and_network_error(self):
        self.client.force_login(create_superuser('a', 'a@example.com', 'pass'))
        url = reverse('admin:core_mailbox_recover_newsletters')
        progress = {'last': 30, 'seen': 30, 'matched': 20, 'skipped': 10, 'total': 50, 'done': False}
        with (
            patch('core.admin_newsletter_recovery.default_mailbox'),
            patch('core.admin_newsletter_recovery.recover_batch', return_value=progress),
        ):
            self.assertEqual(self.client.post(url).status_code, 200)
        response = self.client.get(url)
        self.assertEqual(response.context_data['state']['last'], 30)
        with (
            patch('core.admin_newsletter_recovery.default_mailbox'),
            patch('core.admin_newsletter_recovery.recover_batch', side_effect=MailboxError('Przerwane połączenie')),
        ):
            response = self.client.post(url)
        self.assertTrue(response.context_data['continue_run'])
        self.assertEqual(response.context_data['state']['last'], 30)


class NewsletterListTests(TestCase):
    def test_consents_merge_and_filter_superuser_only(self):
        record_consents('JAN@example.com', premieres=True)
        record_consents('jan@example.com', recruitment=True)
        record_consents('jan@example.com')
        record_consents('other@example.com')
        self.assertEqual(NewsletterConsent.objects.count(), 2)
        url = reverse('core:newsletter_list')
        self.client.force_login(create_member('coord', 'Koordynator recenzji'))
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(create_superuser())
        response = self.client.get(url, {'consent': 'general'})
        self.assertContains(response, 'jan@example.com')
        self.assertNotContains(response, 'other@example.com')

    def test_newsletter_sort_is_global_and_has_all_columns(self):
        """Sortowanie obejmuje wszystkie strony listy, a nie tylko bieżącą."""
        NewsletterConsent.objects.bulk_create(
            [NewsletterConsent(email=f'{i:02}@example.test', premieres=i % 2 == 0) for i in range(35)])
        request = RequestFactory().get('/', {'sort': '-email'})
        request.user = create_superuser()
        page = paginate_items(request, NewsletterConsent.objects.all())
        self.assertEqual(page.object_list[0].email, '34@example.test')
        self.assertIn('Zgoda na newsletter o premierach', page.sort_columns)
