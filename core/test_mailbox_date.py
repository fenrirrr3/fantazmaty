from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import MailboxConnection
from core.services.mailbox import MailboxError, read_headers


HEADERS = {'validity': 7, 'rows': [dict(uid=12, sender='a@example.test', subject='Redakcja', date='')],
           'total': 1, 'next_cursor': None, 'previous_cursor': None}


class MailboxDateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('date-admin', 'a@example.test', 'test')
        cls.box = MailboxConnection.objects.create(name='teksty', host='imap.example.test', username='a@example.test', encrypted_password='unused', recruitment_subjects='Test')
        cls.recruitment = MailboxConnection.objects.create(name='próbki', purpose='recruitment', host='imap.example.test', username='b@example.test', encrypted_password='unused')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_both_screens_default_remember_disable_copy_and_reject_changed_date(self):
        for route, module in [('review_bulk_import', 'mailbox'), ('recruitment_mailbox', 'recruitment_mailbox')]:
            with self.subTest(route=route), patch(f'core.views.{module}.read_headers', return_value=deepcopy(HEADERS)) as read:
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
                    response = self.client.post(url, {**payload, 'sent_since': '2026-11-02', 'action': 'download', 'selection': token, 'uids': [12], 'selected': [12]})
                    self.assertEqual(response.status_code, 400)
                    fetch.assert_not_called()
                read.reset_mock()
                response = self.client.post(url, {**payload, "sent_since": "bad"})
                self.assertEqual(response.status_code, 400)
                read.assert_not_called()
                self.client.post(
                    url, {"action": "headers", "roles": ["all"], "sent_since": "2026-11-01"}
                )
                self.assertEqual(read.call_args.kwargs["sent_since"], "")
                page = self.client.get(url)
                self.assertFalse(page.context["date_form"]["date_filter_enabled"].value())
                self.assertContains(page, 'value="2026-11-01"')

    @patch("core.services.mailbox.imaplib.IMAP4_SSL")
    def test_sent_since_in_all_search_paths_includes_boundary_and_excludes_older(self, imap):
        client = imap.return_value
        client.select.return_value = ("OK", [b"3"])
        client.response.return_value = ("UIDVALIDITY", [b"7"])

        def uid(command, *args):
            if command == "SEARCH":
                return (
                    "OK",
                    [b"13 14"]
                    if "SENTSINCE" in args and args[args.index("SENTSINCE") + 1] == "02-Oct-2026"
                    else [b"12 13 14"],
                )
            return "OK", [
                (
                    f"1 (UID {uid} BODY[])".encode(),
                    f"Subject: Redakcja\r\nDate: {day} Oct 2026 12:00:00 +0200\r\n\r\n".encode(),
                )
                for uid, day in [(12, 1), (13, 2), (14, 3)]
                if str(uid) in args[0].split(",")
            ]

        client.uid.side_effect = uid
        with patch.object(self.box, "get_password", return_value="test"):
            for options in (
                {},
                {"subject_filter": "Test"},
                {"recruitment_roles": ["editors", "reviewers"]},
            ):
                result = read_headers(self.box, sent_since="2026-10-02", **options)
                self.assertEqual([row["uid"] for row in result["rows"]], [14, 13])
            result = read_headers(self.box)
            self.assertEqual([row["uid"] for row in result["rows"]], [14, 13, 12])
        client.select.assert_called_with('"INBOX"', readonly=True)
        self.assertFalse(
            any(call.args[0] in ("STORE", "EXPUNGE") for call in client.uid.call_args_list)
        )

    @patch("core.services.mailbox.imaplib.IMAP4_SSL")
    def test_invalid_date_never_connects(self, imap):
        with self.assertRaises(MailboxError):
            read_headers(self.box, sent_since="2026-02-31")
        imap.assert_not_called()
