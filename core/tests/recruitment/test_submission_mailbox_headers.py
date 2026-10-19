"""Ekran pobierania zgłoszeń tekstów z poczty: lista nagłówków zapamiętana w sesji, filtry tematu i daty, kopiowanie adresów i dostęp tylko dla superużytkownika."""
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.admin import MailboxConnectionForm
from authors.models import BlacklistEntry
from core.models import MailboxConnection, MailboxDownload, MailboxHeaderSnapshot
from core.services.mailbox import MailboxError
from core.services.mailbox_import import default_mailbox, mailbox_key, parse_message
from core.views.mailbox import HEADERS_SESSION_KEY, load_token
from fantazmaty.testing import create_superuser

from .support import SubmissionMailboxMixin, named_mail


class SubmissionHeadersTests(SubmissionMailboxMixin, TestCase):
    def test_headers_survive_refresh_navigation_and_remain_private(self):
        page = self.fetch_headers(subject_filter='Test', clean='on', convert='on', rebuild='on')
        self.assertEqual(page.context['result']['rows'][0]['uid'], 12)
        with patch('core.views.mailbox.read_headers') as read:
            self.client.get(reverse('core:home'))
            for _ in range(2):
                page = self.client.get(self.url)
                self.assertContains(page, 'Opowieść')
                self.assertEqual(page.context['subject_filter'], 'Test')
                self.assertTrue(page.context['clean'] and page.context['convert'] and page.context['rebuild'])
            read.assert_not_called()
        saved = self.client.session[HEADERS_SESSION_KEY]
        self.assertNotIn('password', str(saved))
        self.assertNotIn('author_message', str(saved))
        other = create_superuser('other')
        client = Client()
        client.force_login(other)
        self.assertIsNone(client.get(self.url).context['result'])

    def test_next_fetch_replaces_snapshot_and_keeps_pagination(self):
        page = self.fetch_headers()
        cursor = load_token(page.context['result']['next_cursor'], self.user, mailbox_key(self.box), 'mailbox-cursor')
        self.assertEqual(cursor['cursor']['boundary'], 12)
        self.headers['rows'][0].update(uid=11, subject='Starsza wiadomość')
        with patch('core.views.mailbox.read_headers', return_value=deepcopy(self.headers)) as read:
            response = self.client.post(self.url, {'action': 'headers', 'cursor': page.context['result']['next_cursor']})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(read.call_args.args[1], cursor['cursor'])
        page = self.client.get(self.url)
        self.assertEqual(page.context['result']['rows'][0]['uid'], 11)
        selection = load_token(page.context['selection'], self.user, mailbox_key(self.box), 'mailbox-selection')
        self.assertEqual(selection['uids'], [11])

    def test_failed_fetch_does_not_erase_previous_headers(self):
        self.fetch_headers()
        with patch('core.views.mailbox.read_headers', side_effect=MailboxError('Brak połączenia')):
            response = self.client.post(self.url, {'action': 'headers'})
            self.assertContains(response, 'Brak połączenia', status_code=400)
            self.assertEqual(response.context['result']['rows'][0]['uid'], 12)
        self.assertEqual(self.client.get(self.url).context['result']['rows'][0]['uid'], 12)

    def test_changed_mailbox_invalidates_snapshot(self):
        self.fetch_headers()
        self.box.folder = 'Archiwum'
        self.box.save()
        with patch('core.views.mailbox.read_headers') as read:
            self.assertIsNone(self.client.get(self.url).context['result'])
            read.assert_not_called()
        self.assertNotIn(HEADERS_SESSION_KEY, self.client.session)

    def test_refresh_after_hour_renews_selection_and_cursor(self):
        self.fetch_headers()
        later = time.time() + 7200
        with patch('django.core.signing.time.time', return_value=later), patch('core.views.mailbox.read_headers') as read:
            page = self.client.get(self.url)
            for token, salt in ((page.context['selection'], 'mailbox-selection'),
                                (page.context['result']['next_cursor'], 'mailbox-cursor')):
                load_token(token, self.user, mailbox_key(self.box), salt)
            read.assert_not_called()

    def test_non_superuser_cannot_read_saved_headers_or_download(self):
        self.fetch_headers()
        self.user.is_superuser = False
        self.user.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b'anna@example.com', response.content)
        member = get_user_model().objects.create_user('member', 'member@example.com', 'password')
        self.client.force_login(member)
        with patch('core.views.mailbox.fetch_messages') as fetch:
            self.assertEqual(self.client.post(self.url, self.payload()).status_code, 403)
            fetch.assert_not_called()

    @patch('core.views.mailbox.read_headers')
    def test_default_box_and_filtered_headers(self, read):
        MailboxDownload.objects.create(mailbox_key=mailbox_key(self.box), uid_validity=7, uid=12)
        read.return_value = {'rows': [], 'validity': 7, 'total': 0}
        response = self.client.get(self.url)
        read.assert_not_called()
        self.assertNotContains(response, 'name="mailbox"')
        self.assertContains(response, 'name="clean" checked')
        self.client.post(self.url, {'action': 'headers'})
        self.assertEqual(list(read.call_args.kwargs['excluded'](7)), [12])
        self.client.post(self.url, {'action': 'headers', 'show_downloaded': 'on'})
        self.assertIsNone(read.call_args.kwargs['excluded'])

    def test_ambiguous_box(self):
        MailboxConnection.objects.create(name='TEKSTY', host='imap.example.com', username='other',
                                         encrypted_password='unused')
        with self.assertRaises(MailboxError):
            default_mailbox()

    def test_admin_subject_field_and_filter_choice_visible(self):
        self.box.recruitment_subjects = 'Na pokład, psubraty\nDrugi'
        self.box.save()
        self.assertIn('recruitment_subjects', MailboxConnectionForm().fields)
        response = self.client.get(self.url)
        self.assertContains(response, 'Na pokład, psubraty')
        self.assertContains(response, 'name="subject_filter"')

    @patch('core.services.mailbox_email_copy.sender_emails', return_value=['b@example.test'])
    @patch('core.views.mailbox.read_headers')
    def test_mail_copy_keeps_show_downloaded_filter(self, read, copy):
        read.return_value = dict(validity=7, rows=[dict(uid=12, sender='b@example.test', subject='Tytuł', date='')],
                                 total=1, next_cursor=None, previous_cursor=None)
        response = self.client.post(self.url, {'action': 'headers', 'show_downloaded': 'on'}, follow=True)
        token = response.context['selection']
        response = self.client.post(self.url, {'action': 'copy_emails', 'show_downloaded': 'on', 'selection': token})
        self.assertEqual(response.json(), {'emails': ['b@example.test']})
        self.assertIsNone(copy.call_args.kwargs['excluded'])
        self.assertEqual(copy.call_args.kwargs['subject_filter'], '')
        self.assertFalse(MailboxDownload.objects.exists())


class MailboxDateFilterTests(TestCase):
    HEADERS = {'validity': 7, 'rows': [dict(uid=12, sender='a@example.test', subject='Redakcja', date='')],
               'total': 1, 'next_cursor': None, 'previous_cursor': None}

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('date-admin', 'a@example.test')
        MailboxConnection.objects.create(name='teksty', host='imap.example.test', username='a@example.test',
                                         encrypted_password='unused', recruitment_subjects='Test')
        MailboxConnection.objects.create(name='próbki', purpose='recruitment', host='imap.example.test',
                                         username='b@example.test', encrypted_password='unused')

    def test_both_screens_default_remember_disable_copy_and_reject_changed_date(self):
        """Data „wysłane od” jest domyślnie włączona, zapamiętana, przekazana do kopiowania i chroniona podpisem wyboru."""
        self.client.force_login(self.admin)
        for route, module in [('review_bulk_import', 'mailbox'), ('recruitment_mailbox', 'recruitment_mailbox')]:
            with self.subTest(route=route), patch(f'core.views.{module}.read_headers',
                                                  return_value=deepcopy(self.HEADERS)) as read:
                url = reverse('core:' + route)
                page = self.client.get(url)
                self.assertContains(page, 'value="2026-10-02"')
                self.assertTrue(page.context['date_form']['date_filter_enabled'].value())
                if module == 'recruitment_mailbox':
                    self.assertEqual(read.call_args.kwargs['sent_since'], '2026-10-02')
                payload = {'action': 'headers', 'roles': ['all'], 'date_filter_enabled': 'on', 'sent_since': '2026-11-01'}
                self.assertEqual(self.client.post(url, payload).status_code, 302)
                self.assertEqual(read.call_args.kwargs['sent_since'], '2026-11-01')
                page = self.client.get(url)
                self.assertContains(page, 'value="2026-11-01"')
                token = page.context['selection']
                with patch('core.services.mailbox_email_copy.sender_emails', return_value=[]) as copy:
                    response = self.client.post(url, {**payload, 'action': 'copy_emails', 'selection': token})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(copy.call_args.kwargs['sent_since'], '2026-11-01')
                with patch(f'core.views.{module}.fetch_messages') as fetch:
                    response = self.client.post(url, {**payload, 'sent_since': '2026-11-02', 'action': 'download',
                                                      'selection': token, 'uids': [12], 'selected': [12]})
                    self.assertEqual(response.status_code, 400)
                    fetch.assert_not_called()
                read.reset_mock()
                response = self.client.post(url, {**payload, 'sent_since': 'bad'})
                self.assertEqual(response.status_code, 400)
                read.assert_not_called()
                self.client.post(url, {'action': 'headers', 'roles': ['all'], 'sent_since': '2026-11-01'})
                self.assertEqual(read.call_args.kwargs['sent_since'], '')
                page = self.client.get(url)
                self.assertFalse(page.context['date_form']['date_filter_enabled'].value())
                self.assertContains(page, 'value="2026-11-01"')


class SubmissionSnapshotTests(SubmissionMailboxMixin, TestCase):
    """Nagłówki z nocnego pobierania (MailboxHeaderSnapshot) na ekranie pobierania zgłoszeń."""
    ROWS = [{'uid': 13, 'sender': 'nowa@example.com', 'subject': 'Nowa', 'date': ''},
            {'uid': 12, 'sender': 'anna@example.com', 'subject': 'Zaimportowana rano', 'date': ''},
            {'uid': 11, 'sender': 'Zły <zly@example.com>', 'subject': 'Z czarnej listy', 'date': ''}]

    def snapshot(self, fetched_at, key=None, error=''):
        result = {} if error else {'validity': 7, 'rows': deepcopy(self.ROWS), 'total': 30, 'previous_cursor': None,
                                   'next_cursor': {'anchor': 13, 'validity': 7, 'direction': 'older', 'boundary': 11}}
        return MailboxHeaderSnapshot.objects.update_or_create(mailbox=self.box, defaults={
            'mailbox_key': key or mailbox_key(self.box), 'sent_since': '2026-10-02', 'result': result,
            'error': error, 'fetched_at': fetched_at})[0]

    @patch('core.views.mailbox.fetch_messages')
    def test_get_shows_snapshot_with_current_marks_and_signed_tokens(self, fetch):
        """Bez własnych nagłówków strona pokazuje migawkę z czasem lokalnym, aktualnymi znacznikami i działającymi tokenami."""
        self.snapshot(datetime(2026, 10, 19, 1, 0, tzinfo=dt_timezone.utc))
        MailboxDownload.objects.create(mailbox_key=mailbox_key(self.box), uid_validity=7, uid=12)
        BlacklistEntry.objects.create(email='zly@example.com')
        with patch('core.views.mailbox.read_headers') as read:
            page = self.client.get(self.url)
            read.assert_not_called()
        self.assertContains(page, 'Nagłówki pobrane automatycznie: 19 października 2026, 03:00.')
        marks = {row['uid']: (row['downloaded'], row['blacklisted']) for row in page.context['result']['rows']}
        self.assertEqual(marks, {13: (False, False), 12: (True, False), 11: (False, True)})
        key = mailbox_key(self.box)
        self.assertEqual(load_token(page.context['result']['next_cursor'], self.user, key, 'mailbox-cursor')['cursor']['boundary'], 11)
        self.assertEqual(load_token(page.context['selection'], self.user, key, 'mailbox-selection')['uids'], [13, 12, 11])
        fetch.side_effect = lambda *args: [parse_message(13, named_mail())]
        preview = self.client.post(self.url, {'action': 'download', 'selection': page.context['selection'], 'uids': [13],
            'date_filter_enabled': 'on', 'sent_since': '2026-10-02'})
        self.assertEqual(preview.status_code, 200, preview.context.get('skipped_errors'))
        self.assertContains(preview, 'A. M. Nowak')
        self.assertIn('preview_token', preview.context)

    def test_snapshot_versus_session_headers(self):
        """Migawka zastępuje tylko brak lub starsze nagłówki z sesji; ręczne pobranie wygrywa, inna skrzynka jest pomijana."""
        manual_at = timezone.now() - timedelta(hours=2)
        cases = (
            # (opis, ręczne pobranie przed GET, migawka względem niego, klucz, błąd, UID na stronie, informacja o migawce)
            ('brak sesji', False, 1, None, '', 13, True),
            ('starsza sesja', True, 1, None, '', 13, True),
            ('nowsza sesja', True, -1, None, '', 12, False),
            ('inna skrzynka', False, 1, 'inny-klucz', '', None, False),
            ('błąd nocny', True, 1, None, 'Serwer pocztowy nie odpowiada.', 12, False),
        )
        for name, manual, shift, key, error, uid, automatic in cases:
            with self.subTest(name):
                self.client.logout()
                self.client.force_login(self.user)
                MailboxHeaderSnapshot.objects.all().delete()
                if manual:
                    with patch('core.views.mailbox.timezone.now', return_value=manual_at):
                        self.fetch_headers()
                self.snapshot(manual_at + timedelta(hours=shift), key=key, error=error)
                with patch('core.views.mailbox.read_headers') as read:
                    page = self.client.get(self.url)
                    read.assert_not_called()
                rows = (page.context['result'] or {}).get('rows', [])
                self.assertEqual(rows[0]['uid'] if rows else None, uid)
                self.assertEqual(bool(page.context.get('automatic_fetch')), automatic)
                if error:
                    self.assertContains(page, 'Nocne pobieranie nagłówków')
                    self.assertContains(page, error)
                    continue
                if automatic:
                    # Ręczne pobranie dalej nadpisuje nagłówki z migawki.
                    page = self.fetch_headers()
                    self.assertEqual(page.context['result']['rows'][0]['uid'], 12)
                    self.assertNotContains(page, 'pobrane automatycznie')
