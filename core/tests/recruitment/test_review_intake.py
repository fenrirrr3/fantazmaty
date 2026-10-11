"""Ręczne przyjmowanie zgłoszeń tekstów: wklejanie wielu rekordów z podglądem, formularz pojedynczego zgłoszenia, zapis zgód i prywatność wiadomości autora."""
from html.parser import HTMLParser
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.forms import ReviewBulkImportForm
from core.intake_forms import SingleReviewForm
from core.models import NewsletterConsent
from core.services.review_import_parser import encode_submission
from core.services.reviews import import_reviews
from fantazmaty.testing import FormControls, create_member, create_superuser
from texts.models import Anthology, Review

from .support import LEGACY_EXAMPLE, NAMED_BODY, NAMED_MESSAGE, SUBMISSION_FIELDS, TemporaryActivitySpoolMixin


class IntakePanels(HTMLParser):
    """Zwijane panele formularzy przyjmowania (<details class="intake-panel">)."""

    def __init__(self, content):
        super().__init__()
        self.panels = []
        self.feed(content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'details' and 'intake-panel' in attrs.get('class', ''):
            self.panels.append(attrs)


class ReviewIntakeTests(TemporaryActivitySpoolMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin', 'admin@example.com', 'password')
        cls.coordinator = create_member('coord', 'Koordynator recenzji')
        cls.book = Anthology.objects.create(title='Nabór')
        cls.bulk_url = reverse('core:review_bulk_submit')
        cls.create_url = reverse('core:review_create')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_named_bulk_preview_then_commit(self):
        second = (NAMED_BODY.replace('Anna Maria', 'Jan').replace('Nowak-Kowalska', 'Autor')
                  .replace('ANNA@example.com', 'jan@example.com').replace('[Opowieść; część druga]', 'Drugi tytuł')
                  .replace('premierach, naborach', 'naborach'))
        data = {'anthology': self.book.pk, 'records': NAMED_BODY + '\n\n' + second, 'import_action': 'preview'}
        response = self.client.post(self.bulk_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(NewsletterConsent.objects.exists())
        controls = FormControls(self.bulk_url)
        controls.feed(response.content.decode())
        data.update(preview_token=controls.fields['preview_token'], import_action='import')
        self.assertEqual(self.client.post(self.bulk_url, data).status_code, 302)
        self.assertEqual(Review.objects.count(), 2)
        first = Review.objects.get(email='anna@example.com')
        self.assertEqual(first.author_first_name, 'Anna Maria')
        self.assertEqual(first.author_pseudonym, 'A. M. Nowak')
        self.assertEqual(first.author_message, NAMED_MESSAGE)
        self.assertEqual(first.content_warnings, 'przemoc; śmierć')
        self.assertTrue(NewsletterConsent.objects.get(email='anna@example.com').premieres)
        self.assertFalse(NewsletterConsent.objects.get(email='jan@example.com').premieres)

    def test_invalid_second_submission_blocks_batch_with_correct_line(self):
        second = NAMED_BODY.replace('Nazwisko: Nowak-Kowalska', 'Nazwisko:')
        data = {'anthology': self.book.pk, 'records': NAMED_BODY + '\n\n' + second, 'import_action': 'preview'}
        response = self.client.post(self.bulk_url, data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, f'Wiersz {len(NAMED_BODY.splitlines()) + 2}:', status_code=400)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(NewsletterConsent.objects.exists())

    def test_old_and_new_formats_save_warnings_messages_and_consents_only_on_import(self):
        records = (
            'Ala Autor;Stary;fantasy;37930;ala@example.com;123456789;Nabór;premierach, naborach\n'
            + encode_submission(['Ewa Autor', 'Nowy', 'fantasy', 'PRZEMOC', '1234', 'ewa@example.com', '', 'naborach', ''])
            + '\n' + encode_submission(list(SUBMISSION_FIELDS))
        )
        form = ReviewBulkImportForm({'anthology': self.book.pk, 'records': records}, user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(NewsletterConsent.objects.exists())
        self.assertFalse(Review.objects.exists())
        import_reviews(user=self.admin, form=form)
        self.assertEqual(Review.objects.get(title='Stary').content_warnings, '')
        self.assertEqual(Review.objects.get(title='Stary').length, 37930)
        self.assertEqual(Review.objects.get(title='Nowy').content_warnings, 'przemoc')
        review = Review.objects.get(title='Opowieść')
        self.assertEqual(review.author_message, SUBMISSION_FIELDS[8])
        self.assertEqual(review.content_warnings, 'przemoc')
        consent = NewsletterConsent.objects.get(email='jan@example.com')
        self.assertTrue(consent.premieres and consent.recruitment)
        response = self.client.post(self.bulk_url, {
            'anthology': self.book.pk, 'records': 'Jan Inny;Trzeci;fantasy;99;j@example.com;;Nabór;premierach'})
        self.assertContains(response, 'intake-stack', status_code=response.status_code)
        self.assertNotContains(response, 'intake-columns', status_code=response.status_code)
        self.assertNotContains(response, 'Ostrzeżenia: –', status_code=response.status_code)

    def test_invalid_batch_and_failed_save_do_not_leave_consents(self):
        records = encode_submission(list(SUBMISSION_FIELDS))
        form = ReviewBulkImportForm(data={'anthology': self.book.pk, 'records': records + '\nNiepoprawne'}, user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertFalse(NewsletterConsent.objects.exists())
        form = ReviewBulkImportForm(data={'anthology': self.book.pk, 'records': records}, user=self.admin)
        with patch('core.services.newsletters.record_consents', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                import_reviews(user=self.admin, form=form)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(NewsletterConsent.objects.exists())

    def test_anthology_mismatch_rejected(self):
        """Rekord z innej antologii niż wybrana jest odrzucany zamiast trafić do złego naboru."""
        book = Anthology.objects.create(title='Inna antologia')
        form = ReviewBulkImportForm({'anthology': book.pk, 'records': LEGACY_EXAMPLE}, user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('nie odpowiada', str(form.errors))
        book.title = 'Na pokład, psubraty'
        book.save()
        form = ReviewBulkImportForm({'anthology': book.pk, 'records': LEGACY_EXAMPLE}, user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)

    def test_single_form_matches_new_fields_and_saves_them(self):
        form = SingleReviewForm()
        expected = ['author_first_name', 'author_last_name', 'author_pseudonym', 'title', 'genre',
                    'content_warnings', 'length', 'email', 'phone_number', 'newsletter_premieres',
                    'newsletter_recruitment', 'author_message']
        self.assertEqual([name for name in form.fields if name in expected], expected)
        self.assertEqual(form.fields['length'].label, 'Liczba znaków ze spacjami')
        self.assertNotIn('confirm_existing_text', form.fields)
        response = self.client.get(self.create_url)
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
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        review = Review.objects.get()
        self.assertEqual(review.author_pseudonym, 'Pseudonim')
        self.assertEqual(review.phone_number, '123456789')
        self.assertEqual(review.author_message, data['author_message'])
        consent = NewsletterConsent.objects.get()
        self.assertTrue(consent.premieres and consent.recruitment)
        # Bez pól opcjonalnych i z jedną zgodą zapisuje się tylko ta zgoda.
        data = {'author_first_name': 'Jan', 'author_last_name': 'Autor', 'email': 'jan@example.com',
                'title': 'Drugi pojedynczy', 'genre': 'fantasy', 'length': 1234, 'anthology': self.book.pk,
                'content_warnings': 'PRZEMOC', 'author_message': 'Tylko administrator', 'newsletter_premieres': 'on'}
        response = self.client.post(self.create_url, data)
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None))
        consent = NewsletterConsent.objects.get(email='jan@example.com')
        self.assertTrue(consent.premieres)
        self.assertFalse(consent.recruitment)
        self.assertEqual(Review.objects.get(title='Drugi pojedynczy').author_message, 'Tylko administrator')

    def test_panels_closed_initially_bulk_first_and_errors_open(self):
        response = self.client.get(self.create_url)
        panels = IntakePanels(response.content).panels
        self.assertEqual(len(panels), 2)
        self.assertTrue(all('open' not in p for p in panels))
        self.assertLess(response.content.index('Wiele zgłoszeń'.encode()),
                        response.content.index('Pojedyncze zgłoszenie'.encode()))
        self.assertContains(response, 'core/author-search.js')
        self.assertNotContains(self.client.get(reverse('core:home')), 'core/author-search.js')
        response = self.client.post(self.create_url, {'title': 'Błąd'})
        self.assertIn('open', IntakePanels(response.content).panels[1])

    def test_author_message_private_and_escaped(self):
        """Wiadomość autora widzi tylko superużytkownik, i to jako tekst (bez wykonania HTML)."""
        review = Review.objects.create(anthology=self.book, title='Test',
                                       author_message='<script>private-marker</script>', length=1234)
        url = reverse('core:assigned_review_detail', args=[review.pk])
        self.client.force_login(self.coordinator)
        response = self.client.get(url)
        self.assertNotContains(response, 'private-marker')
        self.assertNotIn('author_message', response.context)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(url), '&lt;script&gt;private-marker&lt;/script&gt;')
