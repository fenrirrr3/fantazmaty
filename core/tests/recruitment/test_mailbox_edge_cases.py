"""Luki pokrycia poczty: limity paczki zgłoszeń, wiadomości bez odcisku, konflikty powiadomień i parsowanie próbek."""
from copy import deepcopy
from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from django.test import SimpleTestCase, TestCase, tag

from core.models import NewsletterConsent, Recruitment
from core.recruitment_message import form_fields
from core.services.mailbox import MailboxError
from core.services.mailbox_import import (_package_messages, package_messages, parse_message,
                                          prepare_forms)
from core.services.newsletters import record_consents
from core.services.recruitment_decisions import set_all_decisions
from core.services.recruitment_samples import parse_sample, plain_body
from fantazmaty.testing.data import sample

from .support import FORM_BODY, RecruitmentMailboxMixin, SubmissionMailboxMixin, csv_mail

LIMIT = 150 * 1024 * 1024
TOO_BIG = LIMIT + 1


def big_zip(name, size):
    """Archiwum z jednym plikiem zer o podanym rozmiarze po rozpakowaniu (mały po kompresji)."""
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
        archive.writestr(name, bytes(size))
    stream.seek(0)
    return stream


class SubmissionPackageLimitTests(SimpleTestCase):
    def setUp(self):
        self.message = parse_message(12, csv_mail())

    @tag('slow')
    def test_package_over_150_mb_is_refused_for_every_output_kind(self):
        """Przebudowany DOCX, wynik konwersji i paczka z wielu wiadomości mają ten sam limit 150 MB."""
        converted = big_zip('document.docx', 1)
        with ZipFile(converted, 'a', ZIP_DEFLATED) as archive:
            archive.writestr('document.pdf', bytes(TOO_BIG))
        converted.seek(0)
        cases = {
            'przebudowa bez konwersji': (dict(convert=False), (BytesIO(bytes(TOO_BIG)), 'docx', '')),
            'konwersja do PDF i EPUB': (dict(convert=True), (converted, 'zip', '')),
        }
        for name, (options, result) in cases.items():
            with self.subTest(name), patch('core.services.mailbox_import.convert_document', return_value=result):
                with self.assertRaisesMessage(MailboxError, 'Wynik przekracza 150 MB'):
                    _package_messages([self.message], clean=False, rebuild=True, allow_rebuild_omissions=True,
                                      **options)
        with self.subTest('paczka wielu wiadomości'), \
                patch('core.services.mailbox_import._package_messages',
                      return_value=big_zip('folder/01_tekst.docx', TOO_BIG)):
            skipped = []
            with self.assertRaisesMessage(MailboxError, 'Wynik przekracza 150 MB'):
                package_messages([self.message], skipped_errors=skipped, accepted=[], rebuild_warnings=[])
            self.assertEqual(skipped, [])


class SubmissionFormsWithoutFingerprintTests(SubmissionMailboxMixin, TestCase):
    def test_message_without_fingerprint_is_still_grouped_into_form(self):
        """Wiadomość bez odcisku (np. z dawnej sesji) trafia do formularza, ale nie jest uznana za pobraną."""
        message = parse_message(12, csv_mail())
        message.pop('fingerprint')
        forms = prepare_forms([message], self.box, 7, self.user)
        self.assertEqual(len(forms), 1)
        self.assertFalse(message['downloaded'])
        self.assertEqual(forms[0].data['anthology'], self.book.pk)
        self.assertTrue(forms[0].is_valid(), forms[0].errors)


class SubmissionMailboxViewGapTests(SubmissionMailboxMixin, TestCase):
    def test_cursor_from_other_filters_starts_from_newest_messages(self):
        page = self.fetch_headers()
        cursor = page.context['result']['next_cursor']
        with patch('core.views.mailbox.read_headers', return_value=deepcopy(self.headers)) as read:
            response = self.client.post(self.url, {'action': 'headers', 'cursor': cursor, 'show_downloaded': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(read.call_args.args[1])
        self.assertIsNone(read.call_args.kwargs['excluded'])


class RecruitmentMailboxGapTests(RecruitmentMailboxMixin, TestCase):
    def test_second_notification_for_already_notified_mail_is_conflict(self):
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample()]):
            self.assertEqual(self.client.post(self.url, self.payload('set_notified', notified='yes')).status_code, 200)
            response = self.client.post(self.url, self.payload('set_notified', notified='no'))
        self.assertEqual(response.status_code, 409)
        self.assertIn('Status powiadomienia zmienił się', response.json()['error'])
        self.assertTrue(Recruitment.objects.get().notified)

    def test_header_list_hides_messages_already_added_as_applications(self):
        excluded = self.headers.call_args.kwargs['excluded']
        self.assertEqual(excluded(7), set())
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample()]):
            self.client.post(self.url, self.payload('add'))
        self.assertEqual(excluded(7), {12})
        self.assertEqual(excluded(8), set())

    def test_unknown_decision_is_refused(self):
        record = Recruitment.objects.create(first_name='Jan', last_name='Nowak', email='jan@example.test')
        with self.assertRaisesMessage(ValueError, 'Nieprawidłowa decyzja'):
            set_all_decisions(record, 'maybe')


class RecruitmentSampleParsingTests(SimpleTestCase):
    def message(self, body, subtype='plain', sender='Kandydat <candidate@example.test>'):
        msg = EmailMessage()
        msg['From'] = sender
        msg['Subject'] = 'Zgłoszenie'
        msg.set_content(body, subtype=subtype)
        return msg

    def test_html_body_drops_script_only_document_and_unsafe_links(self):
        cases = {
            'dokument złożony z samej ramki': ('<iframe src="https://remote.invalid/"></iframe>', ''),
            'link javascript bez adresu w treści': ('<p><a href="javascript:alert(1)">Kliknij</a> tutaj</p>',
                                                    'Kliknij tutaj'),
            'bezpieczny link z adresem': ('<p><a href="https://example.test/">Portfolio</a></p>',
                                          'Portfolio (https://example.test/)'),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(plain_body(self.message(body, 'html')), expected)

    def test_overlong_identity_fields_are_left_empty_instead_of_truncated(self):
        """Za długie imię lub adres nie są obcinane: rekord dostaje puste pole, treść pozostaje w wiadomości."""
        long_email = 'a' * 64 + '@' + ('b' * 63 + '.') * 3 + 'com'
        body = FORM_BODY.replace('Maria Anna Nowak-Kowalska', 'Anna ' * 70).replace('Candidate@Example.test',
                                                                                     long_email)
        fields = form_fields(body)
        self.assertEqual((fields['name'], fields['email']), ('', ''))
        parsed = parse_sample(self.message('Treść', sender=long_email).as_bytes())
        self.assertEqual(parsed['email'], '')
        self.assertIn(long_email, parsed['mail_sender'])


class NewsletterConsentGapTests(TestCase):
    def test_consent_without_email_is_ignored(self):
        for email in ('', '   ', None):
            with self.subTest(email=email):
                record_consents(email, premieres=True, recruitment=True)
        self.assertFalse(NewsletterConsent.objects.exists())
