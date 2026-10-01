from copy import deepcopy
from email.message import EmailMessage
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from core.models import MailboxConnection, MailboxDownload, NewsletterConsent
from core.services.mailbox import MailboxError
from core.services.mailbox_import import mailbox_key, parse_message, fetch_messages
from core.services.review_import_parser import encode_submission, parse_review_records
from core.views.mailbox import HEADERS_SESSION_KEY, load_token
from texts.models import Anthology, Review


NAMED_BODY = '''Imię: Anna Maria
Nazwisko: Nowak-Kowalska
Pseudonim: A. M. Nowak
Tytuł opowiadania: [Opowieść; część druga]
Gatunek: Fantasy
Ostrzeżenia o treści: PRZEMOC; ŚMIERĆ
Liczba znaków ze spacjami: 37 930
Adres e-mail: ANNA@example.com
Numer telefonu:
Newsletter: premierach, naborach

Wiadomość do redakcji:
Dzień dobry; przesyłam tekst.
Adres e-mail: to jest fragment wiadomości, nie zmiana adresu.
"Dziękuję".'''


def named_mail(body=NAMED_BODY, *, html=False, subject='Nabór: „Test” – Opowieść', reply_to=None):
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = 'sender@example.com'
    if reply_to:
        message['Reply-To'] = reply_to
    message.set_content(body, subtype='html' if html else 'plain')
    message.add_attachment(b'original', maintype='application', subtype='octet-stream', filename='tekst.docx')
    return message.as_bytes()


class NamedSubmissionTests(SimpleTestCase):
    def test_multiple_pasted_named_submissions_preserve_messages(self):
        second = NAMED_BODY.replace('Anna Maria', 'Jan').replace('Nowak-Kowalska', 'Autor').replace('ANNA@example.com', 'jan@example.com').replace('premierach, naborach', 'naborach')
        records, errors = parse_review_records(NAMED_BODY + '\n\n' + second)
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual([row['author_first_name'] for row in records], ['Anna Maria', 'Jan'])
        self.assertEqual(records[0]['author_message'], NAMED_BODY.split('Wiadomość do redakcji:\n')[1])
        self.assertEqual(records[1]['email'], 'jan@example.com')
        self.assertFalse(records[1]['newsletter_premieres'])
        self.assertTrue(records[1]['newsletter_recruitment'])

    def test_email_does_not_silently_import_multiple_submissions(self):
        with self.assertRaises(MailboxError):
            parse_message(1, named_mail(NAMED_BODY + '\n\n' + NAMED_BODY))

    def test_csv_error_identifies_person_instead_of_uid_and_row(self):
        body = 'Anna Nowak;"Niedomknięty tytuł;fantasy;1000;anna@example.com;;Test\n' + '\n'.join('Linia' for _ in range(86))
        with self.assertRaises(MailboxError) as error:
            parse_message(1402, named_mail(body, reply_to='Anna Nowak <anna@example.com>'))
        text = str(error.exception)
        self.assertTrue(text.startswith('Anna Nowak <anna@example.com>:'))
        self.assertIn('niepoprawny zapis cudzysłowów', text)
        self.assertNotIn('Wiadomość 1402', text)
        self.assertNotRegex(text, r'Wiersz\s+\d+')

    def test_attachment_errors_identify_person(self):
        message = EmailMessage()
        message['Reply-To'] = 'Anna Nowak <anna@example.com>'
        message.set_content(NAMED_BODY)
        message.add_attachment(b'', maintype='application', subtype='octet-stream', filename='pusty.docx')
        with self.assertRaises(MailboxError) as error:
            parse_message(1402, message.as_bytes())
        self.assertIn('Anna Nowak <anna@example.com>', str(error.exception))
        self.assertIn('pusty (0 bajtów)', str(error.exception))
        self.assertNotIn('1402', str(error.exception))

    def test_names_pseudonym_semicolons_and_multiline_message(self):
        parsed = parse_message(12, named_mail())
        rows, errors = parse_review_records(parsed['record'])
        self.assertEqual(errors, [])
        row = rows[0]
        self.assertEqual(row['author_first_name'], 'Anna Maria')
        self.assertEqual(row['author_last_name'], 'Nowak-Kowalska')
        self.assertEqual(row['author_pseudonym'], 'A. M. Nowak')
        self.assertEqual(row['title'], '[Opowieść; część druga]')
        self.assertEqual(row['content_warnings'], 'przemoc; śmierć')
        self.assertEqual(row['length'], 37930)
        self.assertEqual(row['email'], 'anna@example.com')
        self.assertTrue(row['newsletter_premieres'] and row['newsletter_recruitment'])
        self.assertEqual(row['author_message'], NAMED_BODY.split('Wiadomość do redakcji:\n')[1])
        self.assertEqual(parsed['anthology'], 'Test')
        self.assertEqual(parsed['folder'], 'Opowieść')
        self.assertEqual(parsed['author_pseudonym'], 'A. M. Nowak')

    def test_manual_and_mail_share_named_parser(self):
        direct, errors = parse_review_records(NAMED_BODY)
        self.assertEqual(errors, [])
        mailed, errors = parse_review_records(parse_message(1, named_mail())['record'])
        self.assertEqual(errors, [])
        self.assertEqual(direct, mailed)

    def test_optional_fields_can_be_empty_or_omitted(self):
        body = '\n'.join(line for line in NAMED_BODY.splitlines()[:11]
            if not line.startswith(('Pseudonim:', 'Ostrzeżenia o treści:', 'Numer telefonu:', 'Newsletter:')))
        row = parse_review_records(parse_message(1, named_mail(body))['record'])[0][0]
        for name in ('author_pseudonym', 'content_warnings', 'phone_number', 'author_message'):
            self.assertEqual(row[name], '')
        self.assertFalse(row['newsletter_premieres'] or row['newsletter_recruitment'])

    def test_case_whitespace_bom_and_crlf(self):
        body = '\ufeff\r\n' + NAMED_BODY.replace('Imię:', '  IMIĘ :').replace('\n', '\r\n')
        row = parse_review_records(body)[0][0]
        self.assertEqual(row['author_first_name'], 'Anna Maria')

    def test_html_message(self):
        from html import escape
        body = '<html><body>' + ''.join('<p>' + escape(line) + '</p>' for line in NAMED_BODY.splitlines()) + '</body></html>'
        parsed = parse_message(1, named_mail(body, html=True))
        row = parse_review_records(parsed['record'])[0][0]
        self.assertEqual(row['author_first_name'], 'Anna Maria')
        self.assertEqual(row['title'], '[Opowieść; część druga]')
        self.assertIn('Dzień dobry; przesyłam tekst.', row['author_message'])

    def test_missing_duplicate_and_unknown_metadata_fail(self):
        for body, expected in (
            (NAMED_BODY.replace('Nazwisko: Nowak-Kowalska', 'Nazwisko:'), 'nazwisko'),
            (NAMED_BODY.replace('Nazwisko: Nowak-Kowalska', ''), 'nazwisko'),
            (NAMED_BODY.replace('Nazwisko:', 'Imię: Jan\nNazwisko:'), 'więcej niż raz'),
            (NAMED_BODY.replace('Gatunek:', 'Gantunek:'), 'nieznana etykieta'),
            (NAMED_BODY.replace('ANNA@example.com', 'nie jest adresem'), 'adres e-mail'),
        ):
            with self.subTest(expected=expected), self.assertRaises(MailboxError) as error:
                parse_message(1, named_mail(body))
            self.assertIn(expected, str(error.exception).lower())

    def test_invalid_length_pseudonym_and_consent_are_rejected(self):
        for body in (
            NAMED_BODY.replace('37 930', '0'),
            NAMED_BODY.replace('37 930', 'tekst'),
            NAMED_BODY.replace('A. M. Nowak', 'x' * 101),
            NAMED_BODY.replace('premierach, naborach', 'wszystko'),
        ):
            with self.subTest(body=body):
                try:
                    parsed = parse_message(1, named_mail(body))
                except MailboxError:
                    continue
                rows, errors = parse_review_records(parsed['record'])
                self.assertTrue(errors)
                self.assertEqual(rows, [])

    def test_optional_message_end_marker(self):
        parsed = parse_message(1, named_mail(NAMED_BODY + '\n--- KONIEC WIADOMOŚCI AUTORA ---\nStopka techniczna'))
        self.assertNotIn('Stopka techniczna', parsed['author_message'])
        self.assertIn('"Dziękuję".', parsed['author_message'])

    def test_subject_supplies_anthology_and_is_required(self):
        with self.assertRaises(MailboxError) as error:
            parse_message(1, named_mail(subject='Autor – Opowieść'))
        self.assertIn('w temacie brakuje', str(error.exception))

    def test_old_mail_formats_still_parse(self):
        cases = (
            ('Jan Autor;Stary;fantasy;1000;jan@example.com;;Test', '', False),
            ('Jan Autor;Stary;fantasy;1000;jan@example.com;;Test;naborach', '', True),
            (encode_submission(['Jan Autor', 'Stary; drugi', 'fantasy', 'PRZEMOC', '1000', 'jan@example.com', '', 'naborach', 'Wiadomość; autora']), 'przemoc', True),
        )
        for body, warnings, recruitment in cases:
            with self.subTest(body=body):
                parsed = parse_message(1, named_mail(body))
                rows, errors = parse_review_records(parsed['record'])
                self.assertEqual(errors, [])
                self.assertEqual(rows[0]['author_last_name'], 'Autor')
                self.assertEqual(rows[0]['content_warnings'], warnings)
                self.assertEqual(rows[0]['newsletter_recruitment'], recruitment)
                self.assertEqual(rows[0]['author_pseudonym'], '')


class ManualNamedSubmissionTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'password')
        self.client.force_login(self.user)
        self.book = Anthology.objects.create(title='Test')
        self.url = reverse('core:review_bulk_submit')

    def test_named_bulk_preview_then_commit(self):
        from core.test_bulk_form_regression import FormControls
        second = NAMED_BODY.replace('Anna Maria', 'Jan').replace('Nowak-Kowalska', 'Autor').replace('ANNA@example.com', 'jan@example.com').replace('[Opowieść; część druga]', 'Drugi tytuł').replace('premierach, naborach', 'naborach')
        data = {'anthology': self.book.pk, 'records': NAMED_BODY + '\n\n' + second, 'import_action': 'preview'}
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(NewsletterConsent.objects.exists())
        controls = FormControls(self.url)
        controls.feed(response.content.decode())
        data.update(preview_token=controls.fields['preview_token'], import_action='import')
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Review.objects.count(), 2)
        first = Review.objects.get(email='anna@example.com')
        self.assertEqual(first.author_first_name, 'Anna Maria')
        self.assertEqual(first.author_pseudonym, 'A. M. Nowak')
        self.assertEqual(first.author_message, NAMED_BODY.split('Wiadomość do redakcji:\n')[1])
        self.assertEqual(first.content_warnings, 'przemoc; śmierć')
        self.assertTrue(NewsletterConsent.objects.get(email='anna@example.com').premieres)
        self.assertFalse(NewsletterConsent.objects.get(email='jan@example.com').premieres)

    def test_invalid_second_submission_blocks_batch_with_correct_line(self):
        second = NAMED_BODY.replace('Nazwisko: Nowak-Kowalska', 'Nazwisko:')
        data = {'anthology': self.book.pk, 'records': NAMED_BODY + '\n\n' + second, 'import_action': 'preview'}
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, f'Wiersz {len(NAMED_BODY.splitlines()) + 2}:', status_code=400)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(NewsletterConsent.objects.exists())

    def test_single_form_matches_new_fields_and_saves_them(self):
        from core.intake_forms import SingleReviewForm
        form = SingleReviewForm()
        expected = ['author_first_name', 'author_last_name', 'author_pseudonym', 'title', 'genre',
                    'content_warnings', 'length', 'email', 'phone_number', 'newsletter_premieres',
                    'newsletter_recruitment', 'author_message']
        self.assertEqual([name for name in form.fields if name in expected], expected)
        self.assertEqual(form.fields['length'].label, 'Liczba znaków ze spacjami')
        self.assertNotIn('confirm_existing_text', form.fields)
        url = reverse('core:review_create')
        response = self.client.get(url)
        self.assertContains(response, '<legend>Newsletter</legend>')
        self.assertContains(response, 'name="newsletter_premieres"', count=1)
        self.assertContains(response, 'name="newsletter_recruitment"', count=1)
        self.assertNotContains(response, 'import-format-example')
        data = {'anthology': self.book.pk, 'author_first_name': 'Anna Maria',
                'author_last_name': 'Nowak-Kowalska', 'author_pseudonym': 'Pseudonim',
                'title': 'Pojedynczy', 'genre': 'fantasy', 'content_warnings': 'PRZEMOC',
                'length': '37930', 'email': 'anna@example.com', 'phone_number': '123456789',
                'newsletter_premieres': 'on', 'newsletter_recruitment': 'on',
                'author_message': 'Pierwszy wiersz;\nDrugi wiersz.'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        review = Review.objects.get()
        self.assertEqual(review.author_pseudonym, 'Pseudonim')
        self.assertEqual(review.phone_number, '123456789')
        self.assertEqual(review.author_message, data['author_message'])
        consent = NewsletterConsent.objects.get()
        self.assertTrue(consent.premieres and consent.recruitment)


class PersistentHeadersTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'password')
        self.client.force_login(self.user)
        self.box = MailboxConnection.objects.create(name='teksty', host='imap.example.com', username='teksty@example.com', encrypted_password='unused', recruitment_subjects='Test')
        self.book = Anthology.objects.create(title='Test')
        self.url = reverse('core:review_bulk_import')
        self.headers = {'validity': 7, 'total': 51, 'rows': [
            {'uid': 12, 'sender': 'anna@example.com', 'date': '1 października 2026, 15:00', 'subject': 'Opowieść', 'message_id': '<test@example.com>'}],
            'next_cursor': {'anchor': 100, 'validity': 7, 'direction': 'older', 'boundary': 12}, 'previous_cursor': None}

    def fetch_headers(self, **options):
        with patch('core.views.mailbox.read_headers', return_value=deepcopy(self.headers)) as read:
            response = self.client.post(self.url, {'action': 'headers', **options})
            self.assertRedirects(response, self.url, fetch_redirect_response=False)
            self.assertEqual(read.call_count, 1)
        return self.client.get(self.url)

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
        other = get_user_model().objects.create_superuser('other', 'other@example.com', 'password')
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
        import time
        self.fetch_headers()
        later = time.time() + 7200
        with patch('django.core.signing.time.time', return_value=later), patch('core.views.mailbox.read_headers') as read:
            page = self.client.get(self.url)
            for token, salt in ((page.context['selection'], 'mailbox-selection'), (page.context['result']['next_cursor'], 'mailbox-cursor')):
                load_token(token, self.user, mailbox_key(self.box), salt)
            read.assert_not_called()

    @patch('core.views.mailbox.fetch_messages')
    def test_preview_confirm_and_refresh_preserve_list_and_import_named_values(self, fetch):
        page = self.fetch_headers(subject_filter='Test')
        raw = named_mail()
        fetch.side_effect = lambda *args: [parse_message(12, raw)]
        payload = {'action': 'download', 'selection': page.context['selection'], 'uids': [12], 'subject_filter': 'Test'}
        preview = self.client.post(self.url, payload)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'A. M. Nowak')
        self.assertEqual(preview.context['result']['rows'][0]['uid'], 12)
        self.assertFalse(Review.objects.exists() or NewsletterConsent.objects.exists())
        payload.update(action='confirm', preview=preview.context['preview_token'], approve='on')
        response = self.client.post(self.url, payload)
        self.assertTrue(response.streaming, response.context.get('error') if response.context else '')
        self.assertTrue(b''.join(response.streaming_content))
        review = Review.objects.get()
        self.assertEqual(review.author_first_name, 'Anna Maria')
        self.assertEqual(review.author_last_name, 'Nowak-Kowalska')
        self.assertEqual(review.author_pseudonym, 'A. M. Nowak')
        self.assertEqual(review.title, '[Opowieść; część druga]')
        self.assertEqual(review.content_warnings, 'przemoc; śmierć')
        self.assertTrue(NewsletterConsent.objects.get().premieres)
        with patch('core.views.mailbox.read_headers') as read:
            page = self.client.get(self.url)
            self.assertTrue(page.context['result']['rows'][0]['downloaded'])
            self.assertEqual(page.context['result']['rows'][0]['uid'], 12)
            read.assert_not_called()
        self.assertEqual(MailboxDownload.objects.count(), 1)

    @patch('core.views.mailbox.fetch_messages')
    def test_mixed_named_and_old_batch_keeps_each_identity(self, fetch):
        self.headers['rows'].append({'uid': 13, 'subject': 'Starszy tekst'})
        page = self.fetch_headers()
        raw_named = named_mail()
        raw_old = named_mail('Jan Autor;Starszy;fantasy;1000;jan@example.com;;Test;naborach')
        fetch.side_effect = lambda *args: [parse_message(12, raw_named), parse_message(13, raw_old)]
        payload = {'action': 'download', 'selection': page.context['selection'], 'uids': [12, 13]}
        preview = self.client.post(self.url, payload)
        self.assertEqual(preview.status_code, 200)
        payload.update(action='confirm', preview=preview.context['preview_token'], approve='on')
        response = self.client.post(self.url, payload)
        self.assertTrue(response.streaming, response.context.get('error') if response.context else '')
        self.assertTrue(b''.join(response.streaming_content))
        self.assertEqual(Review.objects.count(), 2)
        self.assertEqual(Review.objects.get(email='anna@example.com').author_first_name, 'Anna Maria')
        self.assertEqual(Review.objects.get(email='jan@example.com').author_last_name, 'Autor')

    def test_non_superuser_cannot_read_saved_headers(self):
        self.fetch_headers()
        self.user.is_superuser = False
        self.user.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b'anna@example.com', response.content)

    @patch('core.services.mailbox_import.imaplib.IMAP4_SSL')
    def test_twenty_messages_allowed_and_twenty_one_rejected(self, imap):
        raw = named_mail()
        client = imap.return_value
        client.select.return_value = ('OK', [b'20'])
        client.response.return_value = ('UIDVALIDITY', [b'7'])
        def fetch(command, uid, fields):
            if 'RFC822.SIZE' in fields:
                return 'OK', [f'1 (UID {uid} RFC822.SIZE {len(raw)})'.encode()]
            return 'OK', [(f'1 (UID {uid} BODY[] {{{len(raw)}}})'.encode(), raw)]
        client.uid.side_effect = fetch
        self.box.set_password('unused')
        self.assertEqual(len(fetch_messages(self.box, 7, list(range(1, 21)))), 20)
        self.assertTrue(client.select.call_args.kwargs['readonly'])
        imap.reset_mock()
        with self.assertRaises(MailboxError) as error:
            fetch_messages(self.box, 7, list(range(1, 22)))
        self.assertIn('od 1 do 20', str(error.exception))
        imap.assert_not_called()

    @patch('core.views.mailbox.fetch_messages')
    def test_validation_error_identifies_correct_person_in_mixed_batch(self, fetch):
        self.headers['rows'].append({'uid': 13, 'subject': 'Błędne zgłoszenie'})
        page = self.fetch_headers()
        fetch.return_value = [
            parse_message(12, named_mail(reply_to='Anna Nowak <anna@example.com>')),
            parse_message(13, named_mail(NAMED_BODY.replace('37 930', 'abc'), reply_to='Jan Kowalski <jan@example.com>')),
        ]
        response = self.client.post(self.url, {'action': 'download', 'selection': page.context['selection'], 'uids': [12, 13]})
        errors = response.context['hard_errors']
        self.assertTrue(errors)
        self.assertTrue(all('Jan Kowalski <jan@example.com>' in error for error in errors))
        self.assertTrue(all('Wiersz' not in error for error in errors))
        self.assertFalse(Review.objects.exists())
