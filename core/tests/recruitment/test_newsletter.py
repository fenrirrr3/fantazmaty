"""Zgody na newsletter: łączenie zgód, lista dla superużytkownika i odzyskiwanie zgód ze starych wiadomości skrzynki (tylko część tekstowa, tylko do odczytu, partiami z wznawianiem i ponawianiem)."""
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from core.admin_newsletter_recovery import SALT, SESSION_KEY, extract_consent, recover_batch
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

    def test_html_body_and_unreadable_submissions(self):
        """Treść HTML jest zamieniana na tekst; brak treści, błędny adres lub nieznana zgoda dają brak wyniku."""
        html_mail = EmailMessage()
        html_mail.set_content('<p>Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;premierach</p>'
                              '<p>Treść wiadomości: naborach</p>', subtype='html')
        self.assertEqual(extract_consent(html_mail.as_bytes()),
                         ('jan@example.com', {'premieres': True, 'recruitment': False}))
        attachment_only = EmailMessage()
        attachment_only.add_attachment(b'%PDF', maintype='application', subtype='pdf', filename='tekst.pdf')
        self.assertIsNone(extract_consent(attachment_only.as_bytes()))
        self.assertIsNone(extract_consent(message('Jan Nowak;Tytuł;fantasy;1200;a@b;123;Nabór;premierach')))
        self.assertIsNone(extract_consent(message('Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;wszystko')))


def text_part(size, subtype='PLAIN', params='("CHARSET" "utf-8")', encoding='8BIT', disposition='NIL'):
    """Opis części tekstowej w BODYSTRUCTURE (pole 9 to Content-Disposition)."""
    return f'("TEXT" "{subtype}" {params} NIL NIL "{encoding}" {size} 1 NIL {disposition} NIL)'


def structure_reply(structure, uid=12):
    return 'OK', [f'1 (UID {uid} BODYSTRUCTURE {structure})'.encode()]


def body_reply(body, uid=12, section=1):
    return 'OK', [(f'1 (UID {uid} BODY[{section}] {{{len(body)}}}'.encode(), body), b')']


class TextBodyFetchTests(SimpleTestCase):
    """Odczyt samej części tekstowej wiadomości (BODYSTRUCTURE + BODY.PEEK) bez pobierania załączników."""

    def fetch(self, *replies):
        client = MagicMock()
        client.uid.side_effect = list(replies)
        return client, fetch_text_body(client, 12)

    def test_recovery_fetches_body_without_large_attachment(self):
        body = b'Jan Autor;Tytul;fantasy;1000;jan@example.test;123;Nabor;premierach'
        client, raw = self.fetch(
            structure_reply(f'({text_part(len(body))}("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" 90000000 NIL '
                            '("ATTACHMENT" NIL)) "MIXED")'),
            body_reply(body))
        self.assertIn(body, raw)
        self.assertEqual(client.uid.call_args.args[-1], '(UID BODY.PEEK[1]<0.1048577>)')

    def test_plain_preferred_html_fallback_and_text_attachments_skipped(self):
        """Pliki .txt (z nazwą lub jako załącznik) nie są treścią zgłoszenia; bez części plain czytany jest HTML."""
        body = b'<p>Tre\xc5\x9b\xc4\x87</p>'
        named = text_part(10, params='("NAME" "notatki.txt")')
        attached = text_part(10, disposition='("ATTACHMENT" NIL)')
        client, raw = self.fetch(
            structure_reply(f'({named}{attached}({text_part(len(body), "HTML")} "ALTERNATIVE") "MIXED")'),
            body_reply(body, section='3.1'))
        self.assertEqual(client.uid.call_args.args[-1], '(UID BODY.PEEK[3.1]<0.1048577>)')
        self.assertTrue(raw.startswith(b'Content-Type: text/html; charset="utf-8"\r\n'))
        self.assertTrue(raw.endswith(body))
        client, raw = self.fetch(structure_reply(f'({text_part(4, "HTML")}{text_part(4)} "ALTERNATIVE")'),
                                 body_reply(b'abcd', section=2))
        self.assertEqual(client.uid.call_args.args[-1], '(UID BODY.PEEK[2]<0.1048577>)')

    def test_no_text_part_or_too_large_text_is_skipped_without_fetching_body(self):
        for structure in ('("APPLICATION" "PDF" NIL NIL NIL "BASE64" 100 NIL NIL NIL)', 'NIL',
                          text_part(1024 * 1024 + 1)):
            with self.subTest(structure=structure):
                client, raw = self.fetch(structure_reply(structure))
                self.assertIsNone(raw)
                self.assertEqual(client.uid.call_count, 1)

    def test_charset_is_sanitized_and_unknown_transfer_encoding_rejected(self):
        _, raw = self.fetch(structure_reply(text_part(2, params='("CHARSET" "utf-8\\" x")')), body_reply(b'ok'))
        self.assertIn(b'charset="utf-8"', raw)
        _, raw = self.fetch(structure_reply(text_part(2, params='("CHARSET" "iso-8859-2")', encoding='BASE64')),
                            body_reply(b'ok'))
        self.assertTrue(raw.startswith(b'Content-Type: text/plain; charset="iso-8859-2"\r\n'
                                       b'Content-Transfer-Encoding: base64\r\n'))
        with self.assertRaisesMessage(MailboxError, 'Nieobsługiwane kodowanie'):
            self.fetch(structure_reply(text_part(2, encoding='X-UUENCODE')), body_reply(b'ok'))

    def test_malformed_structure_responses(self):
        for reply in (
            ('NO', [b'1 (UID 12 BODYSTRUCTURE NIL)']),
            structure_reply(text_part(2), uid=13),
            ('OK', [b'1 (UID 12 FLAGS ())']),
            ('OK', [b'1 (UID 12 BODYSTRUCTURE ' + b'(' * 1024 * 256 + b')']),
        ):
            with self.subTest(reply=reply[1][0][:40]), self.assertRaisesMessage(MailboxError, 'struktury wiadomości'):
                self.fetch(reply)
        with self.assertRaisesMessage(MailboxError, 'Niepoprawna struktura MIME'):
            self.fetch(structure_reply('(' * 40 + ')' * 40))
        with self.assertRaisesMessage(MailboxError, 'Niepełna struktura MIME'):
            self.fetch(structure_reply('(("TEXT" "PLAIN"'))

    def test_incomplete_or_foreign_body_is_rejected(self):
        structure = structure_reply(text_part(4))
        for reply in (('NO', []), ('OK', [b')']), body_reply(b'abc'), body_reply(b'abcd', uid=13)):
            with self.subTest(reply=reply), self.assertRaises(MailboxError):
                self.fetch(structure, reply)


def recovery_config(security='ssl', pk=1):
    return SimpleNamespace(purpose=MailboxConnection.Purpose.SUBMISSIONS, pk=pk, host='example.com', port=993,
                           security=security, username='teksty@example.com', folder='INBOX',
                           get_password=lambda: 'secret')


def consent_body(number):
    raw = message(f'Jan Nowak;Tytuł;fantasy;1200;jan{number}@example.com;123;Nabór;premierach')
    return BytesParser().parsebytes(raw).get_payload(decode=True)


def recovery_client(bodies, validity=b'10'):
    """Atrapa IMAP dla odzyskiwania zgód; treść None oznacza wiadomość z tekstem większym niż 1 MB."""
    client = MagicMock()
    client.select.return_value = ('OK', [b'1'])
    client.response.return_value = ('UIDVALIDITY', [validity])

    def uid(command, *args):
        if command == 'SEARCH':
            numbers = sorted(bodies)
            if args[1] == 'UID':
                low, high = map(int, args[2].split(':'))
                numbers = [n for n in numbers if low <= n <= high]
            return 'OK', [' '.join(map(str, numbers)).encode()]
        number = int(args[0])
        body = bodies[number]
        if args[-1] == '(UID BODYSTRUCTURE)':
            return structure_reply(text_part(len(body) if body is not None else 2 * 1024 * 1024), uid=number)
        return body_reply(body, uid=number)

    client.uid.side_effect = uid
    return client


@patch('core.admin_newsletter_recovery.imaplib.IMAP4_SSL')
class ConsentRecoveryTests(TestCase):
    def test_readonly_peek_idempotent_snapshot(self, imap):
        client = imap.return_value = recovery_client({1: consent_body(1)})
        config = recovery_config()
        state = recover_batch(config)
        recover_batch(config)
        self.assertTrue(state['done'])
        self.assertEqual(state['matched'], 1)
        self.assertEqual(NewsletterConsent.objects.count(), 1)
        client.select.assert_called_with('"INBOX"', readonly=True)
        self.assertTrue(any('BODY.PEEK' in str(c) for c in client.uid.call_args_list))
        self.assertFalse(any(c.args[0] in ('STORE', 'EXPUNGE') for c in client.uid.call_args_list))
        client.response.return_value = ('UIDVALIDITY', [b'11'])
        with self.assertRaisesMessage(MailboxError, 'Folder zmienił identyfikatory'):
            recover_batch(config, state)

    def test_batches_of_ten_resume_within_snapshot(self, imap):
        """Partia to najwyżej 10 wiadomości; nowe wiadomości po pierwszej partii nie są czytane."""
        bodies = {n: consent_body(n) for n in range(1, 13)}
        client = imap.return_value = recovery_client(bodies)
        config = recovery_config()
        state = recover_batch(config)
        self.assertEqual((state['seen'], state['last'], state['maximum'], state['total'], state['done']),
                         (10, 10, 12, 12, False))
        bodies[13] = consent_body(13)
        state = recover_batch(config, state)
        self.assertEqual((state['seen'], state['matched'], state['last'], state['done']), (12, 12, 12, True))
        self.assertEqual(client.uid.call_args_list[-5].args, ('SEARCH', None, 'UID', '11:12'))
        self.assertEqual(NewsletterConsent.objects.count(), 12)
        calls = client.uid.call_count
        self.assertTrue(recover_batch(config, state)['done'])
        self.assertEqual(client.uid.call_count, calls)

    def test_batch_stops_after_ten_seconds(self, imap):
        imap.return_value = recovery_client({1: consent_body(1), 2: consent_body(2)})
        with patch('core.admin_newsletter_recovery.time.monotonic', side_effect=[0, 11]):
            state = recover_batch(recovery_config())
        self.assertEqual((state['seen'], state['last'], state['done']), (1, 1, False))

    def test_oversized_unrecognized_and_unparsable_messages_are_listed_for_manual_check(self, imap):
        imap.return_value = recovery_client({1: None, 2: b'Zwykla wiadomosc', 3: consent_body(3), 4: b'x'})

        def extract(raw):
            if raw.endswith(b'\r\n\r\nx'):
                raise UnicodeError('nieznane kodowanie')
            return extract_consent(raw)

        with patch('core.admin_newsletter_recovery.extract_consent', side_effect=extract):
            state = recover_batch(recovery_config(), {'last': 0, 'seen': 0, 'matched': 0, 'skipped': 499,
                                                      'skipped_uids': list(range(1000, 1499))})
        self.assertEqual((state['matched'], state['skipped'], state['oversized']), (1, 502, 1))
        self.assertEqual(state['skipped_uids'][-3:], [1, 2, 4])
        self.assertEqual(len(state['skipped_uids']), 500)
        self.assertTrue(state['skipped_uids_truncated'])
        self.assertTrue(NewsletterConsent.objects.filter(email='jan3@example.com').exists())

    def test_configuration_and_server_errors(self, imap):
        config = recovery_config()
        with self.assertRaisesMessage(MailboxError, 'Zmieniono konfigurację skrzynki'):
            recover_batch(config, {'key': 'inna skrzynka'})
        imap.assert_not_called()
        with self.assertRaisesMessage(MailboxError, 'Nieobsługiwany sposób szyfrowania'):
            recover_batch(recovery_config(security='plain'))
        client = imap.return_value = recovery_client({1: consent_body(1)})
        client.logout.side_effect = OSError('closed')
        client.select.return_value = ('NO', [])
        with self.assertRaisesMessage(MailboxError, 'Nie można otworzyć folderu'):
            recover_batch(config)
        client.select.return_value = ('OK', [b'1'])
        client.response.return_value = ('UIDVALIDITY', [None])
        with self.assertRaisesMessage(MailboxError, 'Serwer nie podał UIDVALIDITY'):
            recover_batch(config)
        client.response.return_value = ('UIDVALIDITY', [b'10'])
        client.uid.side_effect = lambda *args: ('NO', [])
        with self.assertRaisesMessage(MailboxError, 'Nie udało się pobrać listy'):
            recover_batch(config)
        imap.side_effect = ConnectionResetError()
        with self.assertRaisesMessage(MailboxError, 'Nie udało się odczytać skrzynki'):
            recover_batch(config)

    def test_starttls_connection(self, imap):
        with patch('core.admin_newsletter_recovery.imaplib.IMAP4') as plain:
            client = plain.return_value = recovery_client({1: consent_body(1)})
            self.assertTrue(recover_batch(recovery_config(security='starttls'))['done'])
        client.starttls.assert_called_once()
        imap.assert_not_called()


class ConsentRecoveryAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin', 'admin@example.com', 'pass')
        cls.url = reverse('admin:core_mailbox_recover_newsletters')

    def setUp(self):
        self.client.force_login(self.admin)

    def cursor(self, user=None, **state):
        return signing.dumps({'last': 5, 'seen': 5, 'matched': 1, 'skipped': 4, 'total': 9, 'done': False,
                              'user': (user or self.admin).pk, **state}, salt=SALT, compress=True)

    def post(self, data=None, **batch):
        with (
            patch('core.admin_newsletter_recovery.default_mailbox'),
            patch('core.admin_newsletter_recovery.recover_batch', **batch) as action,
        ):
            return self.client.post(self.url, data or {}), action

    def test_admin_only_no_get_write_and_csrf(self):
        user = get_user_model().objects.create_user('staff', is_staff=True)
        self.client.force_login(user)
        self.assertIn(self.client.get(self.url).status_code, (302, 403))
        request = RequestFactory().get(self.url)
        request.user = user
        with self.assertRaises(PermissionDenied):
            admin.site._registry[MailboxConnection].recover_newsletters(request)
        self.client.force_login(self.admin)
        with patch('core.admin_newsletter_recovery.recover_batch') as action:
            self.assertEqual(self.client.get(self.url).status_code, 200)
            action.assert_not_called()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url).status_code, 403)

    def test_progress_survives_get_and_network_error(self):
        progress = {'last': 30, 'seen': 30, 'matched': 20, 'skipped': 10, 'total': 50, 'done': False}
        self.assertEqual(self.post(return_value=progress)[0].status_code, 200)
        response = self.client.get(self.url)
        self.assertEqual(response.context_data['state']['last'], 30)
        response, _ = self.post(side_effect=MailboxError('Przerwane połączenie'))
        self.assertTrue(response.context_data['continue_run'])
        self.assertEqual(response.context_data['state']['last'], 30)
        self.assertEqual(response.context_data['retry_delay'], 15000)

    def test_restart_discards_saved_progress(self):
        session = self.client.session
        session[SESSION_KEY] = self.cursor()
        session.save()
        response, action = self.post({'restart': '1', 'cursor': self.cursor()},
                                     return_value={'last': 1, 'seen': 1, 'matched': 1, 'skipped': 0, 'done': True})
        self.assertIsNone(action.call_args.args[1])
        self.assertFalse(response.context_data['continue_run'])
        self.assertContains(response, 'Zakończono.')

    def test_forged_or_foreign_cursor_is_rejected(self):
        session = self.client.session
        session[SESSION_KEY] = 'forged'
        session.save()
        response, action = self.post()
        action.assert_not_called()
        self.assertIn('Sesja narzędzia wygasła', response.context_data['error'])
        self.assertNotIn(SESSION_KEY, self.client.session)
        other = create_superuser('other', 'other@example.com')
        response, action = self.post({'cursor': self.cursor(other)})
        self.assertEqual(response.status_code, 403)
        action.assert_not_called()

    def test_automatic_retry_stops_after_three_failures(self):
        for retries, continues in ((2, True), (3, False)):
            with self.subTest(retries=retries):
                response, _ = self.post({'cursor': self.cursor(retries=retries)}, side_effect=MailboxError('Błąd'))
                self.assertEqual(response.context_data['state']['retries'], retries + 1)
                self.assertEqual(response.context_data['continue_run'], continues)
        response, _ = self.post({'restart': '1'}, side_effect=MailboxError('Brak połączenia'))
        self.assertIsNone(response.context_data['state'])
        self.assertFalse(response.context_data['continue_run'])
        self.assertContains(response, 'Brak połączenia')


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
