"""Ignorowane wiadomości na ekranach „Dodaj do recenzji” i „Pobieranie próbek”: oznaczenie zaznaczonych bez łączenia
ze skrzynką, znikanie z listy i z kolejnych pobrań, pokazywanie na życzenie, przywracanie i odmowy."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, tag

from core.models import MailboxIgnoredMessage, Recruitment, RecruitmentMailSource
from core.services.mailbox_import import mailbox_key
from core.views.mailbox import HEADERS_SESSION_KEY
from core.views.recruitment_mailbox import SESSION_KEY

from .support import RecruitmentMailboxMixin, SubmissionMailboxMixin

ROWS = [{'uid': 13, 'sender': 'Ewa <ewa@example.test>', 'subject': 'Duplikat', 'date': '', 'message_id': '<13@x>'},
        {'uid': 12, 'sender': 'anna@example.com', 'subject': 'Opowieść', 'date': '', 'message_id': '<12@x>'}]


@tag('external')
class SubmissionIgnoredTests(SubmissionMailboxMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.headers.update(rows=deepcopy(ROWS), total=2)

    def post(self, action, uids, page, **extra):
        return self.client.post(self.url, {'action': action, 'selection': page.context['selection'],
                                           'uids': [str(uid) for uid in uids], **extra})

    def test_ignore_hides_message_and_restore_brings_it_back(self):
        """Oznaczenie zapisuje nadawcę, temat i osobę, a lista z sesji od razu chowa wiadomość; z „Pokaż również
        ignorowane” wiadomość wraca z oznaczeniem i można ją przywrócić. Skrzynka nie jest czytana."""
        page = self.fetch_headers()
        with patch('core.views.mailbox.read_headers') as read, patch('core.views.mailbox.fetch_messages') as fetch:
            response = self.post('ignore', [13], page)
            self.assertRedirects(response, self.url, fetch_redirect_response=False)
            self.post('ignore', [13], page)  # ponowne oznaczenie nie tworzy duplikatu
            page = self.client.get(self.url)
            read.assert_not_called()
            fetch.assert_not_called()
        ignored = MailboxIgnoredMessage.objects.get()
        self.assertEqual((ignored.mailbox_key, ignored.uid_validity, ignored.uid, ignored.sender, ignored.subject,
                          ignored.ignored_by), (mailbox_key(self.box), 7, 13, 'Ewa <ewa@example.test>', 'Duplikat', self.user))
        self.assertEqual(([row['uid'] for row in page.context['result']['rows']], page.context['result']['total']), ([12], 1))
        self.assertNotContains(page, 'value="unignore"')
        self.assertContains(page, 'Oznaczono jako ignorowane: 1.')
        page = self.fetch_headers(show_ignored='on')
        self.assertEqual([(row['uid'], row['ignored']) for row in page.context['result']['rows']], [(13, True), (12, False)])
        self.assertContains(page, '<span class="badge mailbox-ignored-badge">Ignorowana</span>', count=1)
        self.assertContains(page, 'name="show_ignored" checked')
        response = self.post('unignore', [13, 12], page, show_ignored='on')
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertFalse(MailboxIgnoredMessage.objects.exists())
        self.assertContains(self.client.get(self.url), 'Przywrócono wiadomości: 2.')

    def test_refused_choices_save_nothing(self):
        page = self.fetch_headers()
        for name, uids, data in (('spoza listy', [99], {}), ('pusty wybór', [], {}), ('nie liczba', ['abc'], {}),
                                 ('obcy wybór', [12], {'selection': 'zmieniony'})):
            with self.subTest(name):
                response = self.post('ignore', uids, page, **data)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['error'])
        self.assertFalse(MailboxIgnoredMessage.objects.exists())

    def test_session_from_before_ignoring_still_works(self):
        """Nagłówki zapisane w sesji przed wdrożeniem (bez opcji ignorowanych) pokazują się jak dotąd."""
        self.fetch_headers()
        session = self.client.session
        del session[HEADERS_SESSION_KEY]['show_ignored']
        session.save()
        page = self.client.get(self.url)
        self.assertEqual([row['uid'] for row in page.context['result']['rows']], [13, 12])
        self.assertEqual(self.post('ignore', [12], page).status_code, 302)


@tag('external')
class RecruitmentIgnoredTests(RecruitmentMailboxMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.headers.return_value = dict(validity=7, rows=deepcopy(ROWS), total=2, next_cursor=None, previous_cursor=None)
        self.page = self.client.post(self.url, {'action': 'headers', 'roles': ['all']}, follow=True)

    def post(self, action, uids, **extra):
        return self.client.post(self.url, {'action': action, 'roles': ['all'], 'selection': self.page.context['selection'],
                                           'selected': [str(uid) for uid in uids], **extra})

    def test_ignore_hides_message_and_restore_brings_it_back(self):
        """Oznaczenie bez łączenia ze skrzynką chowa wiadomość z pobranej listy i z kolejnych pobrań; z „Pokaż również
        ignorowane” wraca ze stanem „Ignorowana” i można ją przywrócić."""
        self.headers.reset_mock()
        with patch('core.views.recruitment_mailbox.fetch_messages') as fetch:
            self.assertEqual(self.post('ignore', [13]).status_code, 302)
            fetch.assert_not_called()
        self.headers.assert_not_called()
        page = self.client.get(self.url)
        self.assertEqual(([row['uid'] for row in page.context['rows']], page.context['total'], page.context['fetched']),
                         ([12], 1, 1))
        self.assertEqual(MailboxIgnoredMessage.objects.get().subject, 'Duplikat')
        self.client.post(self.url, {'action': 'headers', 'roles': ['all']})
        self.assertEqual(self.headers.call_args.kwargs['excluded'](7), {13})
        RecruitmentMailSource.objects.create(recruitment=Recruitment.objects.create(email='k@example.test'),
                                             mailbox_key=mailbox_key(self.box), uid_validity=7, uid=12)
        self.page = self.client.post(self.url, {'action': 'headers', 'roles': ['all'], 'show_ignored': 'on'}, follow=True)
        self.assertEqual(self.headers.call_args.kwargs['excluded'](7), {12})
        self.assertEqual([(row['uid'], row['ignored']) for row in self.page.context['rows']], [(13, True), (12, False)])
        self.assertContains(self.page, 'value="unignore"')
        self.assertContains(self.page, '<span data-recruitment-stored>Nowa</span> <span class="badge mailbox-ignored-badge">Ignorowana</span>')
        self.assertEqual(self.post('unignore', [13], show_ignored='on').status_code, 302)
        self.assertFalse(MailboxIgnoredMessage.objects.exists())

    def test_refused_choices_save_nothing(self):
        for name, uids, data, message in (
            ('spoza listy', [99], {}, 'Wybierz wiadomości z pobranej listy.'),
            ('pusty wybór', [], {}, 'Wybierz wiadomości z pobranej listy.'),
            ('nie liczba', ['abc'], {}, 'Nieprawidłowy wybór wiadomości.'),
            ('inne filtry', [12], {'show_ignored': 'on'}, 'Zmieniono filtry lub skrzynkę. Pobierz nagłówki od początku.'),
        ):
            with self.subTest(name):
                response = self.post('ignore', uids, **data)
                self.assertEqual((response.status_code, response.context['error']), (400, message))
        self.assertFalse(MailboxIgnoredMessage.objects.exists())

    def test_session_from_before_ignoring_still_works(self):
        session = self.client.session
        del session[SESSION_KEY]['show_ignored']
        session.save()
        page = self.client.get(self.url)
        self.assertEqual([row['uid'] for row in page.context['rows']], [13, 12])
        self.assertNotContains(page, 'name="show_ignored" checked')
