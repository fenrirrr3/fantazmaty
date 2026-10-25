"""Odczyt skrzynki IMAP: nagłówki z filtrami tematu, ról i daty, stronicowanie, kopiowanie adresów, limity pobierania, kodowanie nazw folderów i obsługa błędów serwera – zawsze tylko do odczytu."""
import imaplib
import ssl
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import patch

from cryptography.fernet import InvalidToken
from django.test import SimpleTestCase, TestCase, tag

from core.models import MailboxConnection, MailboxDownload
from core.services.mailbox import MailboxError, encode_folder, read_headers
from core.services.mailbox_email_copy import sender_emails
from core.services.mailbox_import import default_mailbox, fetch_messages, mailbox_key, parse_message, prepare_forms
from fantazmaty.testing import create_superuser
from fantazmaty.testing.data import sample

from .support import csv_mail, fake_imap, identity_mail, named_mail, sized_fetch


class SubjectFilterTests(SimpleTestCase):
    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_utf8_subject_search_precedes_pagination_and_is_readonly(self, imap):
        config = MailboxConnection(host='imap.example.com', username='teksty@example.com',
                                   recruitment_subjects='Na pokład, psubraty')
        client = fake_imap(imap, exists=b'100', validity=b'1')
        client.uid.side_effect = [('OK', [b'1 2']), ('OK', [])]
        with patch.object(config, 'get_password', return_value='test'):
            result = read_headers(config, subject_filter='Na pokład, psubraty')
        self.assertEqual(result['total'], 2)
        args = client.uid.call_args_list[0].args
        self.assertEqual(args[:3], ('SEARCH', 'CHARSET', 'UTF-8'))
        self.assertEqual(args[-1], '"Nabór: „Na pokład, psubraty”"'.encode())
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        self.assertIn('BODY.PEEK', client.uid.call_args_list[1].args[-1])

    def test_unknown_filter_does_not_connect(self):
        config = MailboxConnection(recruitment_subjects='Nabór A')
        with patch('core.services.mailbox.imaplib.IMAP4_SSL') as imap:
            with self.assertRaises(MailboxError):
                read_headers(config, subject_filter='inny')
            imap.assert_not_called()


class HeaderReadingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.submissions = MailboxConnection.objects.create(
            name='teksty', host='imap.example.test', username='a@example.test', encrypted_password='unused',
            recruitment_subjects='Test')
        cls.recruitment = MailboxConnection.objects.create(
            name='Próbki', purpose='recruitment', host='imap.example.test', username='audit',
            encrypted_password='unused')

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_sent_since_in_all_search_paths_includes_boundary_and_excludes_older(self, imap):
        client = fake_imap(imap, exists=b'3')

        def uid(command, *args):
            if command == 'SEARCH':
                since = 'SENTSINCE' in args and args[args.index('SENTSINCE') + 1] == '02-Oct-2026'
                return 'OK', [b'13 14'] if since else [b'12 13 14']
            return 'OK', [
                (f'1 (UID {uid} BODY[])'.encode(),
                 f'Subject: Redakcja\r\nDate: {day} Oct 2026 12:00:00 +0200\r\n\r\n'.encode())
                for uid, day in [(12, 1), (13, 2), (14, 3)] if str(uid) in args[0].split(',')
            ]

        client.uid.side_effect = uid
        with patch.object(self.submissions, 'get_password', return_value='test'):
            for options in ({}, {'subject_filter': 'Test'}, {'recruitment_roles': ['editors', 'reviewers']}):
                result = read_headers(self.submissions, sent_since='2026-10-02', **options)
                self.assertEqual([row['uid'] for row in result['rows']], [14, 13])
            result = read_headers(self.submissions)
            self.assertEqual([row['uid'] for row in result['rows']], [14, 13, 12])
        client.select.assert_called_with('"INBOX"', readonly=True)
        self.assertFalse(any(call.args[0] in ('STORE', 'EXPUNGE') for call in client.uid.call_args_list))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_invalid_date_never_connects(self, imap):
        with self.assertRaises(MailboxError):
            read_headers(self.submissions, sent_since='2026-02-31')
        imap.assert_not_called()

    @tag('slow')
    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_exact_roles_counts_pagination_copy_multi_role_and_all(self, imap):
        """Filtr „Korekta” nie łapie „Korekta audiobooków”; liczniki i strony liczone po dokładnych rolach."""
        box = self.recruitment
        client = fake_imap(imap, exists=b'240')
        subjects = {n: ['Korekta audiobooków', 'Korekta', 'Korekta poskładowa',
                        'Korekta, Korekta audiobooków'][n % 4] for n in range(1, 241)}

        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [' '.join(map(str, subjects)).encode()]
            numbers = list(map(int, args[0].split(',')))
            self.assertLessEqual(len(numbers), 200)
            self.assertIn('BODY.PEEK[HEADER.FIELDS', args[1])
            result = []
            for number in numbers:
                message = EmailMessage()
                message['Subject'] = 'Rekrutacja – Jan Kandydat – ' + subjects[number]
                message['From'] = f'person{number}@example.test'
                result.append((f'1 (UID {number} BODY[])'.encode(), message.as_bytes()))
            return 'OK', result

        client.uid.side_effect = uid
        with patch.object(box, 'get_password', return_value='test'):
            first = read_headers(box, recruitment_roles=['proofreaders'])
            self.assertEqual(first['total'], 120)
            self.assertEqual(len(first['rows']), 50)
            second = read_headers(box, first['next_cursor'], recruitment_roles=['proofreaders'])
            third = read_headers(box, second['next_cursor'], recruitment_roles=['proofreaders'])
            self.assertEqual(len(third['rows']), 20)
            self.assertIsNone(third['next_cursor'])
            self.assertEqual({r['uid'] for page in (first, second, third) for r in page['rows']},
                             {n for n in subjects if n % 2})
            back = read_headers(box, second['previous_cursor'], recruitment_roles=['proofreaders'])
            self.assertEqual(back['rows'], first['rows'])
            copied = sender_emails(box, 7, recruitment_roles=['proofreaders'])
            self.assertEqual(set(copied), {f'person{n}@example.test' for n in subjects if n % 2})
            self.assertEqual(read_headers(box, recruitment_roles=[])['total'], 240)
            union = read_headers(box, recruitment_roles=['proofreaders', 'audio_proofreaders'])
            self.assertEqual(union['total'], 180)
            excluded = read_headers(box, recruitment_roles=['proofreaders'], excluded=lambda validity: {1, 3})
            self.assertEqual(excluded['total'], 118)
        client.store.assert_not_called()
        client.expunge.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_audio_only_does_not_leave_empty_pages_or_false_count(self, imap):
        """Wiadomości „Korekta audiobooków” znalezione przez SEARCH „Korekta” nie dają pustych stron ani zawyżonej liczby."""
        client = fake_imap(imap)
        message = EmailMessage()
        message['Subject'] = 'Korekta audiobooków'
        client.uid.side_effect = [('OK', [b'1']), ('OK', [(b'1 (UID 1 BODY[])', message.as_bytes())])]
        with patch.object(self.recruitment, 'get_password', return_value='test'):
            result = read_headers(self.recruitment, recruitment_roles=['proofreaders'])
        self.assertEqual(result, {'validity': 7, 'rows': [], 'total': 0, 'next_cursor': None, 'previous_cursor': None})

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_role_search_unions_results_and_keeps_full_subject(self, imap):
        client = fake_imap(imap, exists=b'3')
        client.uid.side_effect = [
            ('OK', [b'12 13']),
            ('OK', [b'13 14']),
            ('OK', [(f'1 (UID {uid} BODY[])'.encode(),
                     'From: a@example.test\r\nSubject: Redakcja – Recenzje\r\n'.encode('utf-8'))
                    for uid in (12, 13, 14)]),
        ]
        with patch.object(self.recruitment, 'get_password', return_value='test'):
            result = read_headers(self.recruitment, recruitment_roles=['editors', 'reviewers'])
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['rows'][0]['subject'], 'Redakcja – Recenzje')
        self.assertEqual(client.uid.call_args.args[1], '12,13,14')
        client.store.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_all_search_has_no_subject_filter_and_preserves_complete_subject(self, imap):
        client = fake_imap(imap)
        client.uid.side_effect = [('OK', [b'12']), ('OK', [(b'1 (UID 12 BODY[])', sample()['raw'])])]
        with patch.object(self.recruitment, 'get_password', return_value='test'):
            result = read_headers(self.recruitment, recruitment_roles=[])
        self.assertEqual(client.uid.call_args_list[0].args, ('SEARCH', None, 'UID', '1:*'))
        self.assertIn('Korekta audiobooków – zgłoszenie', result['rows'][0]['subject'])

    @patch('core.services.mailbox_email_copy.read_headers')
    def test_copy_mail_addresses_traverses_pages_without_receipts(self, read):
        cursor = {'anchor': 100, 'boundary': 51, 'direction': 'older', 'validity': 7}
        read.side_effect = [
            dict(validity=7, rows=[{'sender': 'A <A@example.test>'}], next_cursor=cursor),
            dict(validity=7, rows=[{'sender': 'a@example.test, b@example.test'}], next_cursor=None),
        ]
        self.assertEqual(sender_emails(self.recruitment, 7, recruitment_roles=['editors']),
                         ['A@example.test', 'b@example.test'])
        self.assertEqual(read.call_args.args[1], cursor)
        self.assertFalse(MailboxDownload.objects.exists())


class MessageFetchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.box = MailboxConnection.objects.create(name='teksty', host='imap.example.com',
                                                   username='teksty@example.com', encrypted_password='unused')
        cls.box.set_password('test')
        cls.box.save()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_fetch_readonly_peek_raw_mode_and_uidvalidity(self, imap):
        raw = csv_mail()
        no_attachments = EmailMessage()
        no_attachments['From'] = 'person@example.test'
        no_attachments.set_content('Zgłaszam się.')
        plain = no_attachments.as_bytes()
        client = fake_imap(imap)
        client.uid.side_effect = [
            ('OK', [f'1 (UID 12 RFC822.SIZE {len(raw)})'.encode()]),
            ('OK', [(b'1 (UID 12 BODY[] {})', raw)]),
            ('OK', [f'1 (UID 12 RFC822.SIZE {len(plain)})'.encode()]),
            ('OK', [(b'1 (UID 12 BODY[])', plain)]),
        ]
        result = fetch_messages(self.box, 7, [12])
        self.assertEqual(result[0]['uid'], 12)
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertIn('BODY.PEEK[]', client.uid.call_args.args[2])
        client.logout.assert_called_once()
        # Tryb surowy (próbki rekrutacyjne) przyjmuje też wiadomość bez załączników.
        self.assertEqual(fetch_messages(self.box, 7, [12], raw_messages=True), [{'uid': 12, 'raw': plain}])
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertIn('BODY.PEEK[]', client.uid.call_args.args[2])
        self.assertFalse(client.store.called)
        self.assertFalse(client.expunge.called)
        with self.assertRaises(MailboxError):
            fetch_messages(self.box, 8, [12])

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_partial_fetch_skips_empty_attachment_and_malformed_submission(self, imap):
        empty = EmailMessage()
        empty['Reply-To'] = 'empty@example.com'
        empty.set_content('Nieprawidłowe dane')
        empty.add_attachment(b'', maintype='application', subtype='octet-stream', filename='pusty.docx')
        raws = {12: empty.as_bytes(), 13: csv_mail(line='Brak zgłoszenia'), 14: csv_mail()}
        client = fake_imap(imap, exists=b'3')
        client.uid.side_effect = lambda command, uid, fields: sized_fetch(raws[int(uid)])(command, uid, fields)
        errors = []
        result = fetch_messages(self.box, 7, [12, 13, 14], errors)
        self.assertEqual([row['uid'] for row in result], [14])
        self.assertEqual(len(errors), 2)
        self.assertIn('empty@example.com', errors[0])
        self.assertIn('pusty (0 bajtów)', errors[0])
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertFalse(client.store.called)
        self.assertFalse(client.expunge.called)

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_submissions_limited_to_twenty_and_raw_samples_to_fifty(self, imap):
        raw = named_mail()
        client = fake_imap(imap, exists=b'50')
        client.uid.side_effect = sized_fetch(raw)
        self.assertEqual(len(fetch_messages(self.box, 7, list(range(1, 21)))), 20)
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        self.assertEqual(len(fetch_messages(self.box, 7, range(1, 51), raw_messages=True, max_messages=50)), 50)
        imap.reset_mock()
        with self.assertRaises(MailboxError) as error:
            fetch_messages(self.box, 7, list(range(1, 22)))
        self.assertIn('od 1 do 20', str(error.exception))
        imap.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_redownload_after_folder_change_without_parsing_submission(self, imap):
        """Wiadomość pobrana z innego folderu (inny klucz skrzynki) jest rozpoznana po odcisku treści."""
        raw = identity_mail()
        meta = parse_message(1, raw, submission=False)
        MailboxDownload.objects.create(mailbox_key='old', uid_validity=1, uid=1, fingerprint=meta['fingerprint'])
        client = fake_imap(imap, validity=b'2')
        client.select.return_value = ('OK', [])
        client.uid.side_effect = [
            ('OK', [f'1 (UID 9 RFC822.SIZE {len(raw)})'.encode()]),
            ('OK', [(b'1 (UID 9 BODY[] {})', raw)]),
        ]
        rows = fetch_messages(self.box, 2, [9])
        self.assertEqual(prepare_forms(rows, self.box, 2, create_superuser()), [])
        self.assertTrue(rows[0]['downloaded'])
        self.assertEqual(rows[0]['files'][0][1], b'original')
        self.assertFalse(client.store.called)
        self.assertFalse(client.expunge.called)

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_each_message_is_parsed_once_with_one_receipt_query(self, imap):
        """Wiadomość jest parsowana raz, a sprawdzenie „już pobrana” (ten sam UID albo odcisk treści) to jedno
        zapytanie – także dla nowej wiadomości; pobrana wcześniej nie dostaje danych zgłoszenia."""
        raw = csv_mail()
        fingerprint = parse_message(12, raw, submission=False)['fingerprint']
        client = fake_imap(imap)
        client.uid.side_effect = sized_fetch(raw)
        for name, receipt, submission in (
            ('nowa', None, True),
            ('ten sam UID', {'mailbox_key': mailbox_key(self.box), 'uid_validity': 7, 'uid': 12}, False),
            ('ten sam odcisk z innej skrzynki', {'mailbox_key': 'inna', 'uid_validity': 1, 'uid': 1,
                                                 'fingerprint': fingerprint}, False),
        ):
            with self.subTest(name):
                MailboxDownload.objects.all().delete()
                if receipt:
                    MailboxDownload.objects.create(**receipt)
                with patch('core.services.mailbox_import.BytesParser', wraps=BytesParser) as parser, \
                        self.assertNumQueries(1):
                    rows = fetch_messages(self.box, 7, [12])
                self.assertEqual(parser.call_count, 1)
                self.assertEqual('record' in rows[0], submission)


class FolderEncodingTests(SimpleTestCase):
    def test_modified_utf7_folder_names(self):
        """Polskie znaki kodowane według RFC 3501 (zmodyfikowane UTF-7), „&” jako „&-”, ASCII bez zmian."""
        self.assertEqual(encode_folder('INBOX'), 'INBOX')
        self.assertEqual(encode_folder('Wysłane'), 'Wys&AUI-ane')
        self.assertEqual(encode_folder('Ważne & pilne'), 'Wa&AXw-ne &- pilne')
        self.assertEqual(encode_folder('Zgłoszenia/żółć'), 'Zg&AUI-oszenia/&AXwA8wFCAQc-')


class HeaderReadingErrorTests(SimpleTestCase):
    """Każdy błąd serwera lub konfiguracji kończy się czytelnym MailboxError, a połączenie jest zamykane."""

    def setUp(self):
        self.config = MailboxConnection(host='imap.example.test', username='teksty@example.test', folder='INBOX',
                                        recruitment_subjects='Test')
        patcher = patch.object(self.config, 'get_password', return_value='secret')
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, imap, *args, **kwargs):
        with self.assertRaises(MailboxError) as error:
            read_headers(self.config, *args, **kwargs)
        return str(error.exception)

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_invalid_roles_or_roles_with_subject_filter_never_connect(self, imap):
        self.assertIn('rolę rekrutacyjną', self.read(imap, recruitment_roles=['nieznana']))
        self.assertIn('rolę rekrutacyjną', self.read(imap, recruitment_roles=['editors'], subject_filter='Test'))
        imap.assert_not_called()

    def test_starttls_and_unsupported_security(self):
        self.config.security = 'starttls'
        with patch('core.services.mailbox.imaplib.IMAP4') as imap:
            imap.error = imaplib.IMAP4_SSL.error
            client = fake_imap(imap)
            client.uid.return_value = ('OK', [b''])
            self.assertEqual(read_headers(self.config)['total'], 0)
        client.starttls.assert_called_once()
        client.logout.assert_called_once()
        self.config.security = 'plain'
        with patch('core.services.mailbox.imaplib.IMAP4_SSL') as imap:
            self.assertIn('Nieobsługiwany sposób szyfrowania', self.read(imap))
        imap.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_login_folder_and_uidvalidity_failures(self, imap):
        client = fake_imap(imap)
        client.login.side_effect = imaplib.IMAP4.error('AUTHENTICATIONFAILED')
        self.assertIn('Nie udało się zalogować', self.read(imap))
        client.login.side_effect = None
        client.select.return_value = ('NO', [b'missing'])
        self.assertIn('Nie można otworzyć folderu', self.read(imap))
        client.select.return_value = ('OK', [b'1'])
        client.response.return_value = ('UIDVALIDITY', [None])
        self.assertIn('UIDVALIDITY', self.read(imap))
        client.response.return_value = ('UIDVALIDITY', [b'0'])
        self.assertIn('Nieprawidłowy zakres', self.read(imap))
        self.assertEqual(client.logout.call_count, 4)
        client.uid.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_stale_or_forged_cursor_is_rejected(self, imap):
        fake_imap(imap)
        cursor = {'anchor': 10, 'validity': 7, 'direction': 'older', 'boundary': 5}
        self.assertIn('Folder został zmieniony', self.read(imap, {**cursor, 'validity': 8}))
        self.assertIn('Folder został zmieniony', self.read(imap, ['not', 'a', 'dict']))
        self.assertIn('Nieprawidłowy kierunek', self.read(imap, {**cursor, 'direction': 'sideways'}))
        self.assertIn('Nieprawidłowy zakres', self.read(imap, {**cursor, 'anchor': '10'}))
        self.assertIn('Nieprawidłowy zakres', self.read(imap, {**cursor, 'boundary': 2 ** 32}))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_rejected_searches(self, imap):
        client = fake_imap(imap)
        client.uid.return_value = ('NO', [b''])
        self.assertIn('wyszukiwanie ról', self.read(imap, recruitment_roles=['editors']))
        self.assertIn('wyszukiwanie tematu w UTF-8', self.read(imap, subject_filter='Test'))
        self.assertIn('Nie udało się odczytać listy', self.read(imap))
        client.uid.side_effect = imaplib.IMAP4.error('BADCHARSET')
        self.assertIn('wyszukiwanie tematu w UTF-8', self.read(imap, subject_filter='Test'))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_connection_and_protocol_errors_are_translated(self, imap):
        for raised, expected in (
            (ssl.SSLError('bad certificate'), 'połączenia TLS'),
            (TimeoutError(), 'nie odpowiada'),
            (ConnectionResetError(), 'nie odpowiada'),
            (imaplib.IMAP4.error('BAD'), 'Nie udało się odczytać danych IMAP'),
        ):
            with self.subTest(raised=type(raised).__name__):
                imap.side_effect = raised
                self.assertIn(expected, self.read(imap))
        imap.side_effect = None
        client = fake_imap(imap)
        client.uid.return_value = ('OK', [b'12 x'])
        self.assertIn('Nie udało się odczytać danych IMAP', self.read(imap))
        with patch.object(self.config, 'get_password', side_effect=InvalidToken):
            self.assertIn('Nie można odszyfrować hasła', self.read(imap))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_failed_logout_does_not_hide_result(self, imap):
        client = fake_imap(imap)
        client.uid.return_value = ('OK', [b''])
        client.logout.side_effect = OSError('closed')
        self.assertEqual(read_headers(self.config)['rows'], [])

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_header_fetch_ignores_foreign_and_malformed_items(self, imap):
        """Elementy odpowiedzi bez treści, z innym UID lub bez UID są pomijane; odmowa FETCH to błąd."""
        client = fake_imap(imap)
        header = b'From: a@example.test\r\nSubject: Autor \xe2\x80\x93 Tytu\xc5\x82\r\n\r\n'
        client.uid.side_effect = [
            ('OK', [b'12 13']),
            ('OK', [b')', (b'1 (UID 99 BODY[])', header), (b'1 (BODY[])', header), (b'1 (UID 13)', None),
                    (b'1 (UID 12 BODY[])', header)]),
        ]
        result = read_headers(self.config)
        self.assertEqual(result['total'], 2)
        self.assertEqual([(row['uid'], row['subject']) for row in result['rows']], [(12, 'Tytuł')])
        client.uid.side_effect = [('OK', [b'12']), ('NO', [])]
        self.assertIn('Nie udało się pobrać nagłówków', self.read(imap))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_cursor_past_remaining_mail_returns_newest_page(self, imap):
        """Gdy po drugiej stronie kursora nic nie zostało (usunięte wiadomości), wraca najnowsza strona."""
        client = fake_imap(imap)
        header = b'From: a@example.test\r\nSubject: S\r\n\r\n'
        client.uid.side_effect = [('OK', [b'20 21']),
                                  ('OK', [(f'1 (UID {n} BODY[])'.encode(), header) for n in (20, 21)])]
        cursor = {'anchor': 30, 'validity': 7, 'direction': 'older', 'boundary': 20}
        result = read_headers(self.config, cursor)
        self.assertEqual([row['uid'] for row in result['rows']], [21, 20])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(client.uid.call_args_list[0].args, ('SEARCH', None, 'UID', '1:30'))


class SenderCopyErrorTests(SimpleTestCase):
    @patch('core.services.mailbox_email_copy.read_headers')
    def test_changed_folder_or_looping_cursor_stops_copy(self, read):
        cursor = {'anchor': 100, 'boundary': 51, 'direction': 'older', 'validity': 7}
        read.return_value = dict(validity=8, rows=[], next_cursor=None)
        with self.assertRaisesMessage(MailboxError, 'Folder pocztowy zmienił się'):
            sender_emails(None, 7)
        read.return_value = dict(validity=7, rows=[{'sender': 'bez adresu'}], next_cursor=cursor)
        with self.assertRaisesMessage(MailboxError, 'Lista wiadomości zmieniła się'):
            sender_emails(None, 7)
        self.assertEqual(read.call_count, 3)


class MessageFetchErrorTests(TestCase):
    """Pobieranie wiadomości: limity rozmiaru, niepełne odpowiedzi i błędy serwera (z listą pominiętych i bez niej)."""

    @classmethod
    def setUpTestData(cls):
        cls.box = MailboxConnection.objects.create(name='teksty', host='imap.example.com',
                                                   username='teksty@example.com', encrypted_password='unused')
        cls.box.set_password('test')
        cls.box.save()

    def fetch(self, *args, **kwargs):
        with self.assertRaises(MailboxError) as error:
            fetch_messages(self.box, 7, *args, **kwargs)
        return str(error.exception)

    def test_default_mailbox_falls_back_to_login_and_requires_exactly_one(self):
        self.box.name = 'Zgłoszenia'
        self.box.save()
        self.assertEqual(default_mailbox(), self.box)
        MailboxConnection.objects.create(name='Druga', host='imap.example.com', username='teksty@example.org')
        with self.assertRaisesMessage(MailboxError, 'jedną aktywną skrzynkę'):
            default_mailbox()

    def test_starttls_and_unsupported_security(self):
        raw = csv_mail()
        self.box.security = 'starttls'
        with patch('core.services.mailbox.imaplib.IMAP4') as imap:
            imap.error = imaplib.IMAP4_SSL.error
            client = fake_imap(imap)
            client.uid.side_effect = sized_fetch(raw)
            self.assertEqual(fetch_messages(self.box, 7, [12], raw_messages=True), [{'uid': 12, 'raw': raw}])
        client.starttls.assert_called_once()
        self.box.security = 'plain'
        with patch('core.services.mailbox.imaplib.IMAP4_SSL') as imap:
            self.assertIn('Nieobsługiwany sposób szyfrowania', self.fetch([12]))
        imap.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_folder_size_and_incomplete_body_errors(self, imap):
        raw = csv_mail()
        client = fake_imap(imap)
        client.select.return_value = ('NO', [])
        self.assertIn('Nie można otworzyć folderu', self.fetch([12]))
        client.select.return_value = ('OK', [b'1'])
        client.uid.side_effect = None
        client.uid.return_value = ('OK', [f'1 (UID 12 RFC822.SIZE {15 * 1024 * 1024 + 1})'.encode()])
        self.assertIn('przekracza 15 MB', self.fetch([12]))
        client.uid.return_value = ('OK', [b'1 (UID 12)'])
        self.assertIn('jest niedostępna', self.fetch([12]))
        client.uid.return_value = None
        client.uid.side_effect = [('OK', [f'1 (UID 12 RFC822.SIZE {len(raw)})'.encode()]),
                                  ('OK', [(b'1 (UID 12 BODY[])', raw[:-1])])]
        self.assertIn('nie udało się pobrać całej wiadomości', self.fetch([12]))
        client.uid.side_effect = [('OK', [f'1 (UID 12 RFC822.SIZE {len(raw)})'.encode()]),
                                  ('OK', [(b'1 (UID 13 BODY[])', raw)])]
        self.assertIn('Serwer zwrócił inną wiadomość', self.fetch([12]))
        self.assertEqual(client.logout.call_count, 5)

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_skipped_errors_collect_unavailable_and_incomplete_messages(self, imap):
        raw = csv_mail()

        def uid(command, number, fields):
            if number == '12':
                return 'OK', [b'1 (UID 12)']
            if number == '13' and 'BODY.PEEK' in fields:
                return 'OK', []
            return sized_fetch(raw)(command, number, fields)

        client = fake_imap(imap)
        client.uid.side_effect = uid
        errors = []
        self.assertEqual([row['uid'] for row in fetch_messages(self.box, 7, [12, 13, 14], errors)], [14])
        self.assertEqual(errors, ['Wiadomość 12: jest niedostępna lub przekracza 15 MB.',
                                  'Wiadomość 13: nie udało się pobrać całej wiadomości.'])

    @patch('core.services.mailbox_import.MAX_BATCH', 100)
    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_batch_size_limit(self, imap):
        client = fake_imap(imap)
        client.uid.return_value = ('OK', [b'1 (UID 12 RFC822.SIZE 101)'])
        self.assertIn('łącznie 50 MB', self.fetch([12], []))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_invalid_submission_raises_without_skip_list(self, imap):
        client = fake_imap(imap)
        client.uid.side_effect = sized_fetch(csv_mail(line='Brak zgłoszenia'))
        self.assertIn('oczekiwano jednego zgłoszenia', self.fetch([12]))

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_connection_errors_are_translated_and_logout_errors_ignored(self, imap):
        imap.side_effect = ConnectionRefusedError()
        self.assertIn('Serwer pocztowy nie odpowiada', self.fetch([12]))
        imap.side_effect = None
        client = fake_imap(imap)
        client.uid.side_effect = sized_fetch(csv_mail())
        client.logout.side_effect = imaplib.IMAP4.error('BYE')
        self.assertEqual(len(fetch_messages(self.box, 7, [12], raw_messages=True)), 1)
