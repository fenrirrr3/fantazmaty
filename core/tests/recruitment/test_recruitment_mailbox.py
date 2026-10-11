"""Skrzynka rekrutacyjna (próbki): filtry ról, podgląd wiadomości jako tekst, dodawanie zgłoszeń, pobieranie załączników w ZIP, decyzje zbiorcze i uprawnienia."""
from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from lxml import html

from core.models import MailboxConnection, MailboxDownload, Recruitment, RecruitmentMailSource
from core.recruitment_message import name_in_subject
from core.recruitment_roles import ROLE_CHOICES
from core.services.mailbox_import import default_mailbox, mailbox_key
from core.services.recruitment_samples import attachment_archive, safe_archive_name
from core.views.recruitment_mailbox import SALT
from fantazmaty.testing import create_member
from fantazmaty.testing.data import sample

from .support import RecruitmentMailboxMixin


class SampleArchiveTests(SimpleTestCase):
    def test_all_formats_inline_nameless_and_forwarded_mail_are_downloaded(self):
        msg = EmailMessage()
        msg['From'] = 'Jan Nowak <jan@example.test>'
        msg.set_content('Treść')
        for name, maintype, subtype in [
            ('stary.doc', 'application', 'msword'),
            ('nowy.docx', 'application', 'octet-stream'),
            ('podglad.pdf', 'application', 'pdf'),
            ('logo.png', 'image', 'png'),
            ('notatki.txt', 'text', 'plain'),
        ]:
            msg.add_attachment(name.encode(), maintype=maintype, subtype=subtype, filename=name, disposition='inline')
        msg.add_attachment(b'unnamed', maintype='application', subtype='pdf')
        forwarded = EmailMessage()
        forwarded['Subject'] = 'Wiadomość'
        forwarded.set_content('Dalej')
        forwarded.add_attachment(b'nested', maintype='application', subtype='pdf', filename='wewnetrzny.pdf')
        msg.add_attachment(forwarded, filename='przekazana.eml')
        downloaded = set()
        with attachment_archive([{'uid': 1, 'raw': msg.as_bytes()}], downloaded_uids=downloaded) as stream, ZipFile(stream) as z:
            names = z.namelist()
            self.assertEqual(len(names), 7)
            self.assertEqual(z.read('Jan Nowak/stary.doc'), b'stary.doc')
            self.assertIn('Jan Nowak/logo.png', names)
            self.assertTrue(any(n.endswith('.pdf') and z.read(n) == b'unnamed' for n in names))
            self.assertIn(b'wewnetrzny.pdf', z.read('Jan Nowak/przekazana.eml'))
        self.assertEqual(downloaded, {1})

    def test_no_attachments_means_no_archive_or_placeholder_folder(self):
        no = sample(1)
        yes = sample(2, attachment=True)
        self.assertIsNone(attachment_archive([no]))
        with attachment_archive([no, yes]) as stream, ZipFile(stream) as z:
            self.assertEqual(z.namelist(), ['Kandydat/próbka.docx'])
        # Link w treści nie jest załącznikiem MIME i nie jest pobierany automatycznie.
        msg = EmailMessage()
        msg.set_content('Plik 1: https://example.test/próbka.doc')
        self.assertIsNone(attachment_archive([{'uid': 3, 'raw': msg.as_bytes()}]))

    def test_names_and_flat_safe_files_without_overwrites(self):
        self.assertEqual(name_in_subject('Nowe zgłoszenie do Fantazmatów – Jan Nowak-Kowalski – Redakcja, Korekta'),
                         'Jan Nowak-Kowalski')
        self.assertEqual(name_in_subject('Nowe zgłoszenie do Fantazmatów – Redakcja'), '')
        self.assertEqual(name_in_subject('Nowe zgłoszenie'), '')
        msg = EmailMessage()
        msg['Subject'] = 'Rekrutacja – Jan Nowak – Ilustracje'
        msg.set_content('Treść')
        for value in (b'one', b'two'):
            msg.add_attachment(value, maintype='application', subtype='octet-stream', filename='../../próbka.docx')
        raw = msg.as_bytes()
        with attachment_archive([{'uid': 1, 'raw': raw}, {'uid': 2, 'raw': raw}]) as stream, ZipFile(stream) as archive:
            names = archive.namelist()
            self.assertEqual(names[:2], ['Jan Nowak/próbka.docx', 'Jan Nowak/próbka (2).docx'])
            self.assertEqual(len(set(names)), 4)
            self.assertEqual(len({name.split('/')[0] for name in names}), 2)
            self.assertTrue(all(len(name.split('/')) == 2 for name in names))
            self.assertEqual([archive.read(name) for name in names], [b'one', b'two', b'one', b'two'])

    def test_reserved_or_empty_names_fall_back_and_failed_archive_is_discarded(self):
        """Nazwy zastrzeżone w Windows i puste dostają nazwę zastępczą; błąd w trakcie pakowania zamyka
        rozpoczęte archiwum i przekazuje błąd dalej."""
        for value, expected in (('CON.docx', 'zapas'), ('lpt1', 'zapas'), (' .. ', 'zapas'),
                                ('a<b>:c.doc', 'a_b__c.doc'), ('CONTRA.docx', 'CONTRA.docx')):
            with self.subTest(value=value):
                self.assertEqual(safe_archive_name(value, 'zapas'), expected)
        streams = []

        class TrackedStream(BytesIO):
            def __init__(self, *args):
                super().__init__(*args)
                streams.append(self)

        with patch('core.services.recruitment_samples.BytesIO', TrackedStream), \
                patch('core.services.recruitment_samples.attachments',
                      side_effect=[[('a.docx', b'x')], RuntimeError('uszkodzony załącznik')]), \
                self.assertRaisesMessage(RuntimeError, 'uszkodzony załącznik'):
            attachment_archive([sample(1), sample(2)])
        self.assertTrue(streams[0].closed)


class RecruitmentSamplesTests(RecruitmentMailboxMixin, TestCase):
    def test_all_is_default_and_headers_survive_return_navigation(self):
        self.assertEqual(self.headers.call_args.kwargs['recruitment_roles'], [])
        self.headers.reset_mock()
        response = self.client.get(self.url)
        self.headers.assert_not_called()
        self.assertContains(response, 'Pobieranie próbek')
        self.assertContains(response, 'value="all" id="id_roles_0" checked')
        self.assertContains(response, 'data-mail-preview')
        self.assertContains(response, '50 wiadomości')

    def test_inline_layout_and_notification(self):
        doc = html.fromstring(self.client.get(self.url).content)
        self.assertTrue(doc.xpath('//div[contains(@class,"mailbox-options")][label/input[@name="show_downloaded"]]'
                                  '//button[@value="headers"]'))
        self.assertTrue(doc.xpath('//select[@data-recruitment-notified]/option[@selected and @value="no"]'))
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample()]):
            response = self.client.post(self.url, self.payload('set_notified', notified='yes'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['notified'])
        record = Recruitment.objects.get()
        self.assertTrue(record.notified)
        self.assertIsNone(record.accepted)
        self.assertFalse(MailboxDownload.objects.exists())
        page = self.client.get(reverse('core:recruitment_list'))
        self.assertContains(page, reverse('core:recruitment_notified', args=[record.pk]))
        self.assertContains(page, 'value="yes" selected')
        # Po imporcie zmiana flagi nie wymaga połączenia ze skrzynką.
        with patch('core.views.recruitment_mailbox.fetch_messages') as fetch:
            response = self.client.post(reverse('core:recruitment_notified', args=[record.pk]),
                                        {'notified': 'no', 'version': response.json()['version']})
            self.assertEqual(response.status_code, 200)
            fetch.assert_not_called()
        record.refresh_from_db()
        self.assertFalse(record.notified)
        self.assertIsNone(record.notified_at)

    @patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample(html=True)])
    def test_preview_is_plain_text_does_not_save_and_requires_signed_selection(self, fetch):
        response = self.client.post(self.url, self.payload('preview', uid=12))
        self.assertTrue(response['Content-Type'].startswith('text/html'))
        body = html.fromstring(response.content.decode('utf-8')).xpath('string(//*[@data-preview-body])')
        self.assertIn('Moje zgłoszenie', body)
        self.assertNotIn('<script', body)
        self.assertNotIn('alert(1)', body)
        self.assertNotIn('tracker', body)
        self.assertFalse(Recruitment.objects.exists())
        self.assertFalse(MailboxDownload.objects.exists())
        response = self.client.post(self.url, self.payload('preview', uid=99))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fetch.call_count, 1)
        # Podgląd bez JS pokazuje ten sam oczyszczony tekst obok kolumny Rola.
        response = self.client.post(self.url, self.payload('preview_html', preview_uid=12))
        self.assertContains(response, 'Moje zgłoszenie')
        self.assertFalse(Recruitment.objects.exists())
        self.assertContains(response, 'data-preview-uid="12"')
        self.assertContains(response, '<th>Podgląd</th>')

    def test_preview_fetches_fresh_mail_and_escapes_literal_markup(self):
        raw = EmailMessage()
        raw['Subject'] = 'Tytuł <img src="https://remote.invalid/title">'
        raw.set_content('Nowa treść z serwera <img src="https://remote.invalid/body" onerror="alert(1)">')
        with patch('core.views.recruitment_mailbox.fetch_messages',
                   return_value=[{'uid': 12, 'raw': raw.as_bytes()}]) as fetch:
            response = self.client.post(self.url, self.payload('preview', uid=12))
        fetch.assert_called_once_with(self.box, 7, {12}, raw_messages=True, max_messages=50)
        self.assertContains(response, 'Nowa treść z serwera')
        self.assertContains(response, '&lt;img')
        self.assertFalse(html.fromstring(response.content.decode('utf-8')).xpath('//img|//script'))
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
        self.assertIsNone(record.accepted)
        self.assertFalse(record.notified)
        self.assertEqual(record.mail_roles, ['editors', 'audio_proofreaders'])
        self.assertEqual(record.first_name, '')
        fetch.reset_mock()
        self.assertEqual(self.client.post(self.url, self.payload(uids=range(1, 52))).status_code, 400)
        fetch.assert_not_called()

    def test_mixed_download_zips_only_attachments_and_registers_every_message(self):
        """Pobranie wiadomości z załącznikiem i bez: ZIP ma tylko załączniki, a znacznik pobrania dostaje tylko ta z plikami."""
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample(12), sample(13, attachment=True)]):
            response = self.client.post(self.url, self.payload('download', uids=(12, 13)))
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(b''.join(response.streaming_content))) as archive:
            self.assertEqual(archive.namelist(), ['Kandydat/próbka.docx'])
            self.assertEqual(archive.read(archive.namelist()[0]), b'sample-document')
        self.assertEqual(Recruitment.objects.count(), 2)
        self.assertEqual(set(MailboxDownload.objects.values_list('uid', flat=True)), {13})
        self.assertIsNone(RecruitmentMailSource.objects.get(uid=12).downloaded_at)
        self.assertIsNotNone(RecruitmentMailSource.objects.get(uid=13).downloaded_at)
        response = self.client.get(reverse('core:recruitment_list'))
        self.assertContains(response, 'candidate@example.test')
        self.assertContains(response, 'Nie podano imienia i nazwiska')

    @patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample()])
    def test_bulk_decision_and_notified_are_independent_and_retry_preserves_both(self, fetch):
        self.assertEqual(self.client.post(self.url, self.payload('bulk', decision='yes')).status_code, 302)
        record = Recruitment.objects.get()
        self.assertTrue(record.accepted)
        self.assertFalse(record.notified)
        self.client.post(self.url, self.payload('bulk', notified='yes'))
        record.refresh_from_db()
        self.assertTrue(record.accepted)
        self.assertTrue(record.notified)
        first_date = record.notified_at
        self.client.post(self.url, self.payload('add'))
        record.refresh_from_db()
        self.assertTrue(record.accepted)
        self.assertEqual(record.notified_at, first_date)
        self.client.post(self.url, self.payload('bulk', decision='no'))
        record.refresh_from_db()
        self.assertFalse(record.accepted)
        self.assertTrue(record.notified)
        self.client.post(self.url, self.payload('bulk', decision='clear', notified='no'))
        record.refresh_from_db()
        self.assertIsNone(record.accepted)
        self.assertFalse(record.notified)
        self.assertIsNone(record.notified_at)
        self.assertEqual(Recruitment.objects.count(), 1)
        response = self.client.get(self.url)
        self.assertContains(response, 'nie wszystkie decyzje')
        self.assertContains(response, 'W bazie')

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_changed_mail_rolls_back_whole_batch_and_invalid_bulk_never_fetches(self, fetch):
        fetch.return_value = [sample()]
        self.client.post(self.url, self.payload())
        original = Recruitment.objects.get().mail_body
        changed = sample()
        changed['raw'] += b'Changed content'
        fetch.return_value = [sample(13), changed]
        response = self.client.post(self.url, self.payload(uids=[12, 13]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Recruitment.objects.count(), 1)
        self.assertEqual(RecruitmentMailSource.objects.count(), 1)
        self.assertEqual(Recruitment.objects.get().mail_body, original)
        fetch.reset_mock()
        self.assertEqual(self.client.post(self.url, self.payload('bulk', decision='invalid')).status_code, 400)
        fetch.assert_not_called()

    def test_refused_requests_explain_why_and_save_nothing(self):
        """Odmowy na ekranie próbek: komunikat zamiast zapisu, bez archiwum i bez śladów pobrania."""
        def mailbox_changed_during_download(*args, **kwargs):
            MailboxConnection.objects.filter(pk=self.box.pk).update(username='inna@example.test')
            return [sample(12, attachment=True)]

        for name, data, fetched, message in (
            ('nieznana operacja', self.payload('archive'), None, 'Nieznana operacja.'),
            ('numer wiadomości nie jest liczbą', self.payload(selected=['abc']), None,
             'Nieprawidłowy wybór wiadomości.'),
            ('wygasły wybór', self.payload(selection='zmieniony'), None, 'Wybór wygasł. Pobierz nagłówki ponownie.'),
            ('nieznana rola', self.payload(roles=['fake']), None, 'Popraw wybór ról.'),
            ('wiadomość zniknęła ze skrzynki', self.payload(), [], 'Nie pobrano wszystkich wybranych wiadomości. Niczego nie zapisano.'),
            ('zmienione ustawienia skrzynki', self.payload('download'), mailbox_changed_during_download,
             'Zmieniono ustawienia skrzynki. Pobierz nagłówki ponownie.'),
        ):
            with self.subTest(name), patch('core.views.recruitment_mailbox.fetch_messages') as fetch:
                fetch.side_effect = fetched if callable(fetched) else lambda *a, **k: fetched
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.context['error'], message)
                self.assertNotIn('Content-Disposition', response)
                self.assertFalse(Recruitment.objects.exists())
                self.assertFalse(MailboxDownload.objects.exists())
                self.assertEqual(fetch.called, fetched is not None)
        MailboxConnection.objects.filter(pk=self.box.pk).update(username=self.box.username)
        response = self.client.post(self.url, self.payload('set_notified', notified='maybe'))
        self.assertEqual((response.status_code, response.json()),
                         (400, {'error': 'Wybierz jedną wiadomość i status Tak lub Nie.'}))

    def test_changed_mailbox_drops_saved_headers_and_missing_mailbox_is_explained(self):
        """Zmiana ustawień skrzynki unieważnia zapamiętane nagłówki; bez jednej aktywnej skrzynki strona to wyjaśnia."""
        self.headers.reset_mock()
        MailboxConnection.objects.filter(pk=self.box.pk).update(folder='Archiwum')
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.headers.call_count, 1)
        MailboxConnection.objects.filter(pk=self.box.pk).update(is_active=False)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('jedną aktywną skrzynkę', response.context['error'])
        self.assertEqual(self.headers.call_count, 1)

    def test_csrf_required_for_writes_and_preview(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for action in ('add', 'download', 'bulk', 'preview'):
            self.assertEqual(client.post(self.url, self.payload(action, uid=12)).status_code, 403)


class RecruitmentMailboxFiltersTests(RecruitmentMailboxMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.member = get_user_model().objects.create_user('ordinary', 'ordinary@example.test', 'test-only')
        cls.submissions = MailboxConnection.objects.create(name='teksty', host='imap.example.test',
                                                           username='teksty@example.test', encrypted_password='unused')

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
        self.assertContains(response, 'type="checkbox" name="roles"', count=len(ROLE_CHOICES) + 1)
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
        fetch.assert_not_called()
        read.assert_not_called()

    def test_separate_mailbox_selection_and_receipt_keys(self):
        """Skrzynka rekrutacyjna o nazwie „teksty” nie zastępuje skrzynki zgłoszeń i ma własny klucz pobrań."""
        self.box.name = 'teksty'
        self.box.save()
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
        self.assertEqual(response.status_code, 400)
        read.assert_not_called()

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_download_without_attachments_still_registers_message(self, fetch):
        raw = b'From: candidate@example.test\r\nSubject: Redakcja\r\n\r\nPlain text, no attachments.'
        fetch.return_value = [{'uid': 12, 'raw': raw}]
        response = self.client.post(self.url, {'roles': ['editors', 'reviewers'], 'action': 'download',
                                               'selection': self.token(), 'selected': ['12']})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response.streaming)
        self.assertNotIn('Content-Disposition', response)
        self.assertTrue(fetch.call_args.kwargs['raw_messages'])
        self.assertEqual(Recruitment.objects.count(), 1)
        self.assertIsNone(Recruitment.objects.get().accepted)
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.views.recruitment_mailbox.fetch_messages')
    def test_tampered_selection_and_changed_filters_are_rejected(self, fetch):
        for token, selected, roles in [
            (self.token(user=self.member.pk), ['12'], ['editors', 'reviewers']),
            (self.token(), ['99'], ['editors', 'reviewers']),
            (self.token(), ['12'], ['reviewers']),
            (self.token(), [], ['editors', 'reviewers']),
        ]:
            response = self.client.post(self.url, {'action': 'download', 'roles': roles, 'selected': selected,
                                                   'selection': token})
            self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()

    @patch('core.services.mailbox_email_copy.sender_emails', return_value=['a@example.test'])
    def test_copy_endpoint_uses_signed_recruitment_filters(self, copy):
        response = self.client.post(self.url, {'action': 'copy_emails', 'roles': ['editors', 'reviewers'],
                                               'selection': self.token()})
        self.assertEqual(response.json(), {'emails': ['a@example.test']})
        self.assertEqual(copy.call_args.kwargs['recruitment_roles'], ['editors', 'reviewers'])
        self.assertFalse(MailboxDownload.objects.exists())


class RecruiterMailboxAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.recruiter = create_member('recruiter', 'Koordynator rekrutacji')
        cls.coordinator = create_member('coordinator', 'Koordynator redakcji')
        cls.member = create_member('member', 'Redaktor')
        cls.box = MailboxConnection.objects.create(name='Próbki', purpose='recruitment', host='imap.example.test',
                                                   username='test', encrypted_password='unused')
        cls.record = Recruitment.objects.create(applicant_name='Jan Kandydat', mail_roles=['editors', 'proofreaders'])

    @patch('core.views.recruitment_mailbox.read_headers', return_value={
        'validity': 7, 'rows': [{'uid': 12, 'subject': 'Redakcja', 'sender': 'candidate@example.test', 'date': ''}],
        'total': 1, 'next_cursor': None, 'previous_cursor': None})
    def test_recruiter_mailbox_access_detail_link_and_copy_remains_superuser_only(self, headers):
        RecruitmentMailSource.objects.create(recruitment=self.record, mailbox_key=mailbox_key(self.box), uid_validity=7, uid=12)
        detail = reverse('core:recruitment_detail', args=[self.record.pk])
        filters = {'roles': 'all', 'date_filter_enabled': 'on', 'sent_since': '2026-10-02'}
        self.client.force_login(self.recruiter)
        url = reverse('core:recruitment_mailbox')
        page = self.client.get(url)
        self.assertContains(page, detail)
        self.assertNotContains(page, 'Ustawienia skrzynki')
        self.assertContains(self.client.get(reverse('core:home')), url)
        response = self.client.post(url, {'action': 'copy_emails', 'selection': page.context['selection'], **filters})
        self.assertEqual(response.status_code, 403)
        # Ta rola może też dodać inną wiadomość ze skrzynki.
        headers.return_value['rows'][0]['uid'] = 13
        page = self.client.post(url, {'action': 'headers', **filters}, follow=True)
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample(13)]):
            response = self.client.post(url, {'action': 'add', 'selected': ['13'],
                                              'selection': page.context['selection'], **filters})
            self.assertEqual(response.status_code, 302)
        self.assertTrue(RecruitmentMailSource.objects.filter(uid=13).exists())
        for user in (self.coordinator, self.member):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {'action': 'add'}).status_code, 403)
        person = self.recruiter.person_profile
        person.is_active = False
        person.save()
        self.client.force_login(self.recruiter)
        self.assertEqual(self.client.get(url).status_code, 403)
