"""Pobieranie zgłoszeń tekstów z poczty: podgląd bez zapisu, zatwierdzenie z ZIP-em, wykluczanie błędnych wiadomości i ochrona przed zmianą danych między krokami."""
from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch
from zipfile import BadZipFile, ZipFile

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import MailboxConnection, MailboxDownload, NewsletterConsent
from core.services.document_converter import ConversionError, RebuildConfirmationRequired
from core.services.mailbox import MailboxError
from core.services.mailbox_import import _package_messages, parse_message
from texts.models import Anthology, Review

from .support import CSV_LINE, NAMED_BODY, SubmissionMailboxMixin, csv_mail, named_mail, submission_selection


def failing_package(uid, error):
    """Zastępuje pakowanie tak, by wiadomość o danym UID zgłaszała błąd konwersji."""
    def package(messages, *args, **kwargs):
        if messages[0]['uid'] == uid:
            raise error
        return _package_messages(messages, *args, **kwargs)
    return package


class SubmissionDownloadTests(SubmissionMailboxMixin, TestCase):
    def confirm(self, payload, preview):
        payload.update(action='confirm', preview=preview.context['preview_token'], approve='on')
        return self.client.post(self.url, payload)

    @patch('core.views.mailbox.fetch_messages')
    def test_preview_commit_retry(self, fetch):
        raw = csv_mail()
        fetch.side_effect = lambda *args: [parse_message(12, raw)]
        payload = self.payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(Review.objects.count(), 0)
        response = self.confirm(payload, response)
        self.assertEqual(response.status_code, 200, response.context.get('error') if not response.streaming else '')
        data = b''.join(response.streaming_content)
        with ZipFile(BytesIO(data)) as z:
            self.assertEqual(len(z.namelist()), 1)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(MailboxDownload.objects.count(), 1)
        row = Review.objects.get()
        self.assertEqual(row.length, 15204)
        self.assertEqual(row.content_warnings, '')
        response = self.client.post(self.url, payload)
        self.assertTrue(response.streaming)

        # Porównujemy elementy archiwum, nie bajty: wpisy ZIP (także w DOCX) mają znaczniki czasu.
        def members(content):
            with ZipFile(BytesIO(content)) as archive:
                return {info.filename: info.file_size for info in archive.infolist()}

        self.assertEqual(members(b''.join(response.streaming_content)), members(data))
        self.assertEqual(Review.objects.count(), 1)

    @patch('core.views.mailbox.fetch_messages')
    def test_preview_confirm_and_refresh_preserve_list_and_import_named_values(self, fetch):
        page = self.fetch_headers(subject_filter='Test', date_filter_enabled='on', sent_since='2026-10-02')
        raw = named_mail()
        fetch.side_effect = lambda *args: [parse_message(12, raw)]
        payload = {'action': 'download', 'selection': page.context['selection'], 'uids': [12],
                   'subject_filter': 'Test', 'date_filter_enabled': 'on', 'sent_since': '2026-10-02'}
        preview = self.client.post(self.url, payload)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'A. M. Nowak')
        self.assertContains(preview, '<input type="hidden" name="sent_since" value="2026-10-02">', html=True)
        self.assertContains(preview, '<input type="hidden" name="date_filter_enabled" value="on">', html=True)
        self.assertEqual(preview.context['result']['rows'][0]['uid'], 12)
        self.assertFalse(Review.objects.exists() or NewsletterConsent.objects.exists())
        response = self.confirm(payload, preview)
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
        response = self.confirm(payload, preview)
        self.assertTrue(response.streaming, response.context.get('error') if response.context else '')
        self.assertTrue(b''.join(response.streaming_content))
        self.assertEqual(Review.objects.count(), 2)
        self.assertEqual(Review.objects.get(email='anna@example.com').author_first_name, 'Anna Maria')
        self.assertEqual(Review.objects.get(email='jan@example.com').author_last_name, 'Autor')

    @patch('core.views.mailbox.fetch_messages')
    def test_invalid_data_is_excluded_and_only_valid_preview_is_committed(self, fetch):
        messages = {
            12: parse_message(12, csv_mail()),
            13: parse_message(13, csv_mail(line='Jan Test;Błędny;fantasy;0;bad@example.com;;Na pokład, psubraty')),
        }
        fetch.side_effect = lambda config, validity, uids, errors: [messages[uid].copy() for uid in uids]
        payload = self.payload((12, 13))
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected'], [12])
        self.assertTrue(response.context['skipped_errors'])
        self.assertFalse(response.context['hard_errors'])
        self.assertFalse(Review.objects.exists())
        self.assertContains(response, 'Zapisz zgłoszenia i pobierz ZIP')
        payload['uids'] = ['12']
        response = self.confirm(payload, response)
        self.assertTrue(response.streaming)
        with ZipFile(BytesIO(b''.join(response.streaming_content))) as archive:
            self.assertEqual(len(archive.namelist()), 1)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(list(MailboxDownload.objects.values_list('uid', flat=True)), [12])

    @patch('core.views.mailbox.fetch_messages')
    def test_validation_error_identifies_correct_person_in_mixed_batch(self, fetch):
        """Błąd walidacji wskazuje autora błędnej wiadomości, a nie numer wiersza czy pierwszą osobę z paczki."""
        self.headers['rows'].append({'uid': 13, 'subject': 'Błędne zgłoszenie'})
        page = self.fetch_headers()
        fetch.return_value = [
            parse_message(12, named_mail(reply_to='Anna Nowak <anna@example.com>')),
            parse_message(13, named_mail(NAMED_BODY.replace('37 930', 'abc'), reply_to='Jan Kowalski <jan@example.com>')),
        ]
        response = self.client.post(self.url, {'action': 'download', 'selection': page.context['selection'], 'uids': [12, 13]})
        errors = response.context['skipped_errors']
        self.assertTrue(errors)
        self.assertTrue(all('Jan Kowalski <jan@example.com>' in error for error in errors))
        self.assertTrue(all('Wiersz' not in error for error in errors))
        self.assertEqual(response.context['selected'], [12])
        self.assertEqual(response.context['hard_errors'], [])
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_unknown_anthology_does_not_block_valid_submission(self, fetch):
        fetch.return_value = [
            parse_message(12, csv_mail()),
            parse_message(13, csv_mail(line='Jan Test;Inny;fantasy;1000;bad@example.com;;Nieistniejąca')),
        ]
        response = self.client.post(self.url, self.payload((12, 13)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected'], [12])
        self.assertIn('nie znaleziono jednoznacznej antologii', response.context['skipped_errors'][0])

    @patch('core.views.mailbox.fetch_messages')
    def test_conversion_failure_excludes_entire_message_and_all_its_files(self, fetch):
        good = parse_message(12, csv_mail())
        broken = parse_message(13, csv_mail(line='Jan Test;Błąd konwersji;fantasy;1000;bad@example.com;;Na pokład, psubraty'))
        broken['files'].append(('drugi.docx', b'broken'))
        fetch.return_value = [good, broken]
        with patch('core.services.mailbox_import._package_messages',
                   side_effect=failing_package(13, ConversionError('Błąd drugiego załącznika'))):
            response = self.client.post(self.url, self.payload((12, 13)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected'], [12])
        self.assertIn('Błąd drugiego załącznika', response.context['skipped_errors'][0])
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_corrupt_docx_is_excluded_during_preview(self, fetch):
        good = parse_message(12, csv_mail())
        broken = parse_message(13, csv_mail(line='Jan Test;Uszkodzony;fantasy;1000;bad@example.com;;Na pokład, psubraty'))
        broken['files'] = [('uszkodzony.docx', b'this is not a ZIP archive')]
        fetch.return_value = [good, broken]
        payload = self.payload((12, 13))
        payload['clean'] = 'on'
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected'], [12])
        self.assertIn('uszkodzony.docx', response.context['skipped_errors'][0])
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_new_failure_at_confirmation_requires_new_approval(self, fetch):
        messages = {
            12: parse_message(12, csv_mail()),
            13: parse_message(13, csv_mail(line='Jan Test;Drugi;fantasy;1000;second@example.com;;Na pokład, psubraty')),
        }
        fetch.side_effect = lambda config, validity, uids, errors: [messages[uid].copy() for uid in uids]
        payload = self.payload((12, 13))
        preview = self.client.post(self.url, payload)
        with patch('core.services.mailbox_import._package_messages',
                   side_effect=failing_package(13, ConversionError('Nowy błąd konwertera'))):
            response = self.confirm(payload, preview)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['selected'], [12])
        self.assertFalse(Review.objects.exists())
        self.assertFalse(MailboxDownload.objects.exists())
        payload.update(uids=['12'], preview=response.context['preview_token'])
        response = self.client.post(self.url, payload)
        self.assertTrue(response.streaming)
        b''.join(response.streaming_content)
        self.assertEqual(Review.objects.count(), 1)

    @patch('core.views.mailbox.fetch_messages')
    def test_all_invalid_has_errors_but_no_confirm_button(self, fetch):
        fetch.return_value = [parse_message(12, csv_mail(line='Jan Test;Błędny;fantasy;0;bad@example.com;;Na pokład, psubraty'))]
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['skipped_errors'])
        self.assertContains(response, 'Brak poprawnych zgłoszeń', status_code=400)
        self.assertNotContains(response, 'Zapisz zgłoszenia i pobierz ZIP', status_code=400)
        self.assertFalse(Review.objects.exists())

    def test_empty_html_mail_is_reported_without_creating_records(self):
        """Pusta treść HTML kończy się czytelnym błędem 400, bez zgłoszeń i bez znacznika pobrania."""
        message = EmailMessage()
        message['Subject'] = 'Test'
        message.set_content('')
        message.set_type('text/html')
        message.add_attachment(b'notempty', maintype='application', subtype='octet-stream', filename='tekst.docx')
        raw = message.as_bytes()
        with patch('core.views.mailbox.fetch_messages', side_effect=lambda *args: [parse_message(12, raw)]):
            response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'treść HTML jest pusta', status_code=400)
        self.assertEqual(Review.objects.count(), 0)
        self.assertEqual(MailboxDownload.objects.count(), 0)

    @patch('core.views.mailbox.fetch_messages')
    def test_rebuild_omission_warning_still_requires_explicit_acceptance(self, fetch):
        raw = csv_mail()
        fetch.side_effect = lambda *args: [parse_message(12, raw)]

        def package(messages, clean, convert, rebuild, allowed, **kwargs):
            if not allowed:
                raise RebuildConfirmationRequired('Pominięta tabela')
            return _package_messages(messages, False, False, False, **kwargs)

        payload = self.payload()
        with patch('core.services.mailbox_import._package_messages', side_effect=package):
            response = self.client.post(self.url, payload)
            self.assertEqual(response.context['selected'], [12])
            self.assertTrue(response.context['rebuild_warning'])
            self.assertFalse(response.context['skipped_errors'])
            response = self.confirm(payload, response)
            self.assertFalse(response.streaming)
            self.assertFalse(Review.objects.exists())
            payload.update(preview=response.context['preview_token'], allow_rebuild_omissions='on')
            response = self.client.post(self.url, payload)
            self.assertTrue(response.streaming)
            b''.join(response.streaming_content)
            self.assertEqual(Review.objects.count(), 1)

    @patch('core.views.mailbox.fetch_messages')
    def test_conversion_failure_no_records(self, fetch):
        fetch.return_value = [parse_message(12, csv_mail())]
        payload = self.payload()
        preview = self.client.post(self.url, payload)
        with patch('core.views.mailbox.package_messages', side_effect=MailboxError('Niepoprawny plik')):
            self.assertEqual(self.confirm(payload, preview).status_code, 400)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_refused_requests_explain_why_and_import_nothing(self, fetch):
        """Odmowy na ekranie pobierania zgłoszeń: czytelny komunikat, bez archiwum i bez zapisu."""
        fetch.return_value = [parse_message(12, csv_mail())]
        stranger = type('Stranger', (), {'pk': self.user.pk + 1})()
        for name, changes, fetched, message in (
            ('wiadomość spoza listy', {'uids': ['13']}, False, 'Wybierz wiadomości z pobranej listy.'),
            ('numer nie jest liczbą', {'uids': ['abc']}, False, 'Nieprawidłowy wybór wiadomości.'),
            ('zepsuty wybór', {'selection': 'zmieniony'}, False, 'Podgląd wygasł lub ustawienia skrzynki się zmieniły.'),
            ('wybór innej osoby', {'selection': submission_selection(stranger, self.box, (12,))}, False,
             'Podgląd wygasł lub ustawienia skrzynki się zmieniły.'),
            ('nabór usunięty z listy', {'subject_filter': 'Dawny nabór'}, False, 'Wybrany nabór nie jest już dostępny.'),
            ('nieznana operacja', {'action': 'usun'}, False, 'Nieprawidłowa operacja.'),
            ('kopiowanie adresów po zmianie filtrów', {'action': 'copy_emails', 'show_downloaded': 'on'}, False,
             'Zmieniono filtry. Pobierz nagłówki ponownie.'),
        ):
            with self.subTest(name):
                fetch.reset_mock()
                response = self.client.post(self.url, {**self.payload(), **changes})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.context['error'])
                self.assertEqual(fetch.called, fetched)
        with patch('core.views.mailbox.package_messages', side_effect=BadZipFile):
            response = self.client.post(self.url, self.payload())
        self.assertIn('Nie udało się przetworzyć pliku.', response.context['error'])
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_confirmation_stops_when_mailbox_changes_or_import_fails(self, fetch):
        """Zatwierdzenie podglądu: zmiana ustawień skrzynki w trakcie albo odmowa zapisu zgłoszenia
        kończą się komunikatem, bez zgłoszeń i bez znaczników pobrania."""
        raw = csv_mail()

        def mailbox_changed(*args):
            MailboxConnection.objects.filter(pk=self.box.pk).update(folder='Archiwum')
            return [parse_message(12, raw)]

        fetch.side_effect = lambda *args: [parse_message(12, raw)]
        payload = self.payload()
        preview = self.client.post(self.url, payload)
        with patch('core.views.mailbox.import_reviews', side_effect=ValidationError('Zgłoszenie już istnieje.')):
            response = self.confirm(dict(payload), preview)
        self.assertEqual((response.status_code, response.context['error']), (400, 'Zgłoszenie już istnieje.'))
        fetch.side_effect = mailbox_changed
        response = self.confirm(dict(payload), preview)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['error'], 'Ustawienia skrzynki się zmieniły. Pobierz nagłówki ponownie.')
        self.assertFalse(Review.objects.exists())
        self.assertFalse(MailboxDownload.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_changed_content_requires_new_preview(self, fetch):
        fetch.return_value = [parse_message(12, csv_mail())]
        payload = self.payload()
        preview = self.client.post(self.url, payload)
        fetch.return_value = [parse_message(12, csv_mail(line='Julia Moskalik;Zmieniony;fantasy;100;girl@example.com;;Na pokład, psubraty'))]
        self.assertEqual(self.confirm(payload, preview).status_code, 400)
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_non_preparation_anthology_rejected(self, fetch):
        """Antologia gotowa (zmieniona na gotową albo od razu utworzona jako gotowa) nie przyjmuje zgłoszeń z poczty."""
        fetch.return_value = [parse_message(12, csv_mail())]
        self.book.status = 'ready'
        self.book.save()
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 400)
        Anthology.objects.create(title='Gotowa antologia', status=Anthology.Status.READY)
        fetch.return_value = [parse_message(12, csv_mail(line=CSV_LINE.replace('Na pokład, psubraty', 'Gotowa antologia')))]
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 400)
        self.assertFalse(Review.objects.exists())

    @patch('core.views.mailbox.fetch_messages')
    def test_duplicate_warning_requires_confirmation_and_token(self, fetch):
        Review.objects.create(anthology=self.book, title='Potworna Przystan', author_first_name='JULIA',
                              author_last_name='MOSKALIK', email='girl@example.com', length=15204, genre='dark fantasy')
        raw = csv_mail()
        fetch.side_effect = lambda *args: [parse_message(12, raw)]
        payload = self.payload()
        response = self.client.post(self.url, payload)
        self.assertTrue(response.context['forms'][0].import_warnings)
        payload.update(action='confirm', preview=response.context['preview_token'])
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Review.objects.count(), 1)
        payload.update(approve='on', preview=response.context['preview_token'])
        response = self.client.post(self.url, payload)
        self.assertTrue(response.streaming)
        with ZipFile(BytesIO(b''.join(response.streaming_content))) as archive:
            self.assertEqual(len(archive.namelist()), 1)
        self.assertEqual(Review.objects.count(), 2)

