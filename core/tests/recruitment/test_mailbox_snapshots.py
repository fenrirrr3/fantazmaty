"""Nocne pobieranie nagłówków (komenda fetch_mailbox_headers): migawki obu skrzynek, błędy bez danych logowania, kod wyjścia i podgląd w adminie."""
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, tag
from django.urls import reverse

from core.models import MailboxConnection, MailboxDownload, MailboxHeaderSnapshot, Recruitment, RecruitmentMailSource
from core.services.mailbox import MailboxError
from core.services.mailbox_import import mailbox_key
from core.services.mailbox_snapshots import UNEXPECTED_ERROR, refresh_snapshots
from fantazmaty.testing import create_member, create_superuser

from .support import fake_imap

HEADERS = {'validity': 7, 'rows': [{'uid': 12, 'sender': 'a@example.test', 'subject': 'Opowieść', 'date': ''}],
           'total': 1, 'next_cursor': None, 'previous_cursor': None}


class MailboxSnapshotMixin:
    """Aktywna skrzynka zgłoszeń „teksty” i skrzynka rekrutacyjna."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.submissions = MailboxConnection.objects.create(name='teksty', host='imap.example.test',
            username='teksty@example.test', encrypted_password='unused')
        cls.recruitment = MailboxConnection.objects.create(name='rekrutacja', purpose='recruitment',
            host='imap.example.test', username='rekrutacja@example.test', encrypted_password='unused')


@tag('external')
class RefreshSnapshotsTests(MailboxSnapshotMixin, TestCase):
    @patch.object(MailboxConnection, 'get_password', return_value='sekret-hasła')
    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_first_page_without_imported_messages_is_saved_for_both_mailboxes(self, imap, password):
        """Pobierane są same nagłówki (od domyślnej daty, tylko do odczytu) bez wiadomości już pobranych lub dodanych do bazy."""
        client = fake_imap(imap)

        def uid(command, *args):
            if command == 'SEARCH':
                self.assertEqual(args[args.index('SENTSINCE') + 1], '02-Oct-2026')
                return 'OK', [b'12 13 14']
            self.assertIn('BODY.PEEK[HEADER.FIELDS', args[-1])
            return 'OK', [(f'1 (UID {number} BODY[])'.encode(), f'Subject: Redakcja {number}\r\n\r\n'.encode())
                          for number in map(int, args[0].split(','))]

        client.uid.side_effect = uid
        MailboxDownload.objects.create(mailbox_key=mailbox_key(self.submissions), uid_validity=7, uid=12)
        RecruitmentMailSource.objects.create(recruitment=Recruitment.objects.create(email='k@example.test'),
            mailbox_key=mailbox_key(self.recruitment), uid_validity=7, uid=13)
        summary = refresh_snapshots()
        self.assertEqual([(item['label'], item['status'], item['count']) for item in summary],
                         [('Zgłoszenia', 'ok', 2), ('Rekrutacja', 'ok', 2)])
        for box, uids in ((self.submissions, [14, 13]), (self.recruitment, [14, 12])):
            with self.subTest(box=box.name):
                snapshot = MailboxHeaderSnapshot.objects.get(mailbox=box)
                self.assertEqual([row['uid'] for row in snapshot.result['rows']], uids)
                self.assertEqual((snapshot.mailbox_key, snapshot.sent_since, snapshot.error), (mailbox_key(box), '2026-10-02', ''))
        self.assertFalse(any(call.args[0] in ('STORE', 'EXPUNGE') for call in client.uid.call_args_list))

    def test_sender_mailbox_is_never_read(self):
        """Skrzynka „Wysyłka plików autorom” (bez serwera IMAP) jest pomijana, nawet gdy nazywa się „teksty”."""
        sender = MailboxConnection.objects.create(name='teksty', purpose='split_mailing', username='teksty@example.org',
                                                  smtp_host='smtp.example.test', encrypted_password='unused')
        read = []
        with patch('core.services.mailbox_snapshots.read_headers',
                   side_effect=lambda config, **options: read.append(config) or HEADERS):
            summary = refresh_snapshots()
        self.assertEqual([item['status'] for item in summary], ['ok', 'ok'])
        self.assertEqual(read, [self.submissions, self.recruitment])
        self.assertFalse(MailboxHeaderSnapshot.objects.filter(mailbox=sender).exists())

    def test_failure_of_one_mailbox_is_saved_without_credentials_and_other_continues(self):
        """Błąd jednej skrzynki trafia do migawki (bez danych logowania) i czyści jej stare nagłówki; druga skrzynka się odświeża."""
        login = 'Nie udało się zalogować do skrzynki. Sprawdź login, hasło aplikacji i dostęp do IMAP.'
        cases = ((MailboxError(login), login, self.assertNoLogs),
                 (RuntimeError('rekrutacja@example.test sekret-hasła'), UNEXPECTED_ERROR, self.assertLogs))
        for failure, expected, logged in cases:
            with self.subTest(failure=type(failure).__name__):
                MailboxHeaderSnapshot.objects.update_or_create(mailbox=self.recruitment, defaults={
                    'mailbox_key': 'stary', 'result': HEADERS, 'fetched_at': '2026-10-18T01:00:00Z'})

                def read(config, **options):
                    if config.purpose == 'recruitment':
                        raise failure
                    return HEADERS

                with patch('core.services.mailbox_snapshots.read_headers', side_effect=read), \
                        logged('core.services.mailbox_snapshots', 'ERROR'):
                    summary = refresh_snapshots()
                self.assertEqual([item['status'] for item in summary], ['ok', 'error'])
                snapshot = MailboxHeaderSnapshot.objects.get(mailbox=self.recruitment)
                self.assertEqual((snapshot.error, snapshot.result, snapshot.mailbox_key), (expected, {}, mailbox_key(self.recruitment)))
                self.assertEqual(MailboxHeaderSnapshot.objects.get(mailbox=self.submissions).result, HEADERS)


class FetchMailboxHeadersCommandTests(MailboxSnapshotMixin, TestCase):
    def run_command(self, failing=(), empty=()):
        """Uruchamia komendę z atrapą odczytu; skrzynki o przeznaczeniu z `failing` zgłaszają błąd, z `empty` są puste."""
        def read(config, **options):
            if config.purpose in failing:
                raise MailboxError('Serwer pocztowy nie odpowiada.')
            if config.purpose in empty:
                return {**HEADERS, 'rows': [], 'total': 0}
            return HEADERS

        out, err = StringIO(), StringIO()
        with patch('core.services.mailbox_snapshots.read_headers', side_effect=read):
            call_command('fetch_mailbox_headers', stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_summary_and_exit_code(self):
        """Podsumowanie na skrzynkę; kod błędu tylko wtedy, gdy zawiodły wszystkie skrzynki."""
        cases = (
            ((), ['Zgłoszenia: pobrano nagłówki – 1.', 'Rekrutacja: pobrano nagłówki – 1.'], ''),
            (('recruitment',), ['Zgłoszenia: pobrano nagłówki – 1.'], 'Rekrutacja: błąd – Serwer pocztowy nie odpowiada.'),
        )
        for failing, lines, error in cases:
            with self.subTest(failing=failing):
                out, err = self.run_command(failing)
                self.assertEqual(out.splitlines(), lines)
                self.assertEqual(err.strip(), error)
        with self.assertRaisesMessage(CommandError, 'Nie udało się pobrać nagłówków z żadnej skrzynki.'):
            self.run_command(('submissions', 'recruitment'))
        self.assertEqual(MailboxHeaderSnapshot.objects.count(), 2)
        # Pusta skrzynka to poprawny wynik (0 nagłówków), zapisany jako migawka bez błędu.
        out, err = self.run_command(empty=('submissions',))
        self.assertEqual((out.splitlines()[0], err), ('Zgłoszenia: pobrano nagłówki – 0.', ''))
        snapshot = MailboxHeaderSnapshot.objects.get(mailbox=self.submissions)
        self.assertEqual((snapshot.error, snapshot.result['rows']), ('', []))

    def test_missing_mailbox_is_skipped_with_information(self):
        """Brak aktywnej skrzynki rekrutacyjnej: pominięcie z wyjaśnieniem, bez migawki i bez kodu błędu."""
        MailboxConnection.objects.filter(pk=self.recruitment.pk).update(is_active=False)
        out, _ = self.run_command()
        self.assertIn('Rekrutacja: pominięto – Ustaw w panelu administratora jedną aktywną skrzynkę', out)
        self.assertEqual(list(MailboxHeaderSnapshot.objects.values_list('mailbox', flat=True)), [self.submissions.pk])


class MailboxSnapshotAdminTests(MailboxSnapshotMixin, TestCase):
    def test_read_only_for_superuser(self):
        """Superużytkownik tylko ogląda migawki (bez dodawania, zmian i usuwania); inne konta nie mają dostępu."""
        snapshot = MailboxHeaderSnapshot.objects.create(mailbox=self.submissions, mailbox_key='k', result=HEADERS,
                                                        error='', fetched_at='2026-10-19T01:00:00Z')
        self.client.force_login(create_superuser())
        changelist = reverse('admin:core_mailboxheadersnapshot_changelist')
        self.assertContains(self.client.get(changelist), 'teksty')
        page = self.client.get(reverse('admin:core_mailboxheadersnapshot_change', args=[snapshot.pk]))
        self.assertContains(page, 'Nagłówki')
        self.assertNotContains(page, 'name="_save"')
        for url in (reverse('admin:core_mailboxheadersnapshot_add'), reverse('admin:core_mailboxheadersnapshot_delete', args=[snapshot.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(create_member('koordynator', 'Koordynator redakcji'))
        self.assertNotEqual(self.client.get(changelist).status_code, 200)
