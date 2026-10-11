"""Ręczne przyjmowanie zgłoszeń tekstów: wklejanie wielu rekordów z podglądem, formularz pojedynczego zgłoszenia (ostrzeżenia, autor z bazy), formularz recenzji w adminie, zapis zgód, prywatność wiadomości autora i wyszukiwanie w Ekstraktach."""
from html.parser import HTMLParser
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from authors.models import Author
from core.forms import ReviewBulkImportForm
from core.intake_forms import SingleReviewForm
from core.models import NewsletterConsent
from core.review_submission_forms import ReviewAdminForm
from core.services.review_import_parser import encode_submission
from core.services.reviews import import_reviews
from fantazmaty.testing import FormControls, create_member, create_superuser
from texts.models import Anthology, Extract, Review, Text
from workflow.models import WorkflowStage

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
        self.assertRegex(response.content.decode(), r'core/author-search(\.[0-9a-f]+)?\.js')
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



class SingleReviewSafeguardTests(TemporaryActivitySpoolMixin, TestCase):
    """Formularz pojedynczego zgłoszenia: podpisane potwierdzenie ostrzeżeń, autor z bazy i wyścig z zamknięciem naboru."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin', 'admin@example.com', 'password')
        cls.book = Anthology.objects.create(title='Nabór')
        cls.url = reverse('core:review_create')
        cls.blacklisted = Author.objects.create(first_name='Jan', last_name='Zakazany', email='zakazany@example.com',
                                                is_blacklisted=True)
        cls.pseudonymous = Author.objects.create(first_name='Anna', last_name='Prawdziwa', pseudonym='Mroczna',
                                                 email='anna@example.com', phone_number='600700800')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def data(self, **extra):
        return {'anthology': self.book.pk, 'author_first_name': 'Jan', 'author_last_name': 'Zakazany',
                'email': 'zakazany@example.com', 'title': 'Opowieść', 'genre': 'fantasy', 'length': '1234', **extra}

    def test_warning_confirmation_is_bound_to_shown_data(self):
        """Potwierdzenie działa tylko z podpisem wydanym dla tych samych danych; inaczej ostrzeżenie wraca."""
        response = self.client.post(self.url, self.data())
        self.assertContains(response, 'czarnej liście', status_code=400)
        form = response.context['single_form']
        token = form.data['submission_warnings_token']
        self.assertEqual(form.data['confirm_submission_warnings'], '')
        for data in (self.data(title='Inny tytuł', submission_warnings_token=token, confirm_submission_warnings='on'),
                     self.data(submission_warnings_token='podrobiony', confirm_submission_warnings='on'),
                     self.data(submission_warnings_token=token)):
            with self.subTest(data=data):
                self.assertContains(self.client.post(self.url, data), 'czarnej liście', status_code=400)
        self.assertFalse(Review.objects.exists())
        response = self.client.post(self.url, self.data(submission_warnings_token=token, confirm_submission_warnings='on'))
        self.assertEqual(response.status_code, 302)
        review = Review.objects.get()
        self.assertTrue(review.is_hidden)
        self.assertEqual(review.status, Review.Status.REJECTED)

    def test_selected_author_fills_contact_data(self):
        """Autor z bazy: formularz wypełniony pseudonimem, dane kontaktowe i telefon brane z rekordu autora."""
        response = self.client.get(self.url, {'author': self.pseudonymous.pk})
        initial = response.context['single_form'].initial
        self.assertEqual((initial['author_first_name'], initial['author_last_name'], initial['email']),
                         ('Mroczna', '', 'anna@example.com'))
        self.assertEqual(self.client.get(self.url, {'author': '١٢'}).context['single_form'].initial, {})
        response = self.client.post(self.url, self.data(author=self.pseudonymous.pk, author_first_name='',
                                                        author_last_name='', email=''))
        self.assertEqual(response.status_code, 302)
        review = Review.objects.get()
        self.assertEqual((review.author, review.email, review.author_pseudonym, review.phone_number),
                         (self.pseudonymous, 'anna@example.com', 'Mroczna', '600700800'))

    def test_historic_author_without_email_and_missing_manual_data(self):
        historic = Author.objects.create(first_name='Stary', last_name='Autor')
        response = self.client.post(self.url, self.data(author=historic.pk))
        self.assertContains(response, 'Uzupełnij adres e-mail autora historycznego', status_code=400)
        response = self.client.post(self.url, self.data(author_last_name='', email=''))
        self.assertIn('Uzupełnij dane albo wybierz autora z bazy.', response.context['single_form'].errors['email'])
        self.assertFalse(Review.objects.exists())

    def test_anthology_closed_during_save_and_model_validation_error(self):
        data = self.data(email='nowy@example.com', author_last_name='Nowy')
        with patch('core.views.intake.frontend_scope', return_value=Anthology.objects.none()):
            response = self.client.post(self.url, data)
        self.assertContains(response, 'Wybrany nabór nie jest już dostępny', status_code=400)
        with patch('core.views.intake.apply_blacklist', side_effect=ValidationError('Błąd zapisu zgłoszenia.')):
            response = self.client.post(self.url, data)
        self.assertContains(response, 'Błąd zapisu zgłoszenia.', status_code=400)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(NewsletterConsent.objects.exists())

    def test_author_lookup_without_javascript_and_suggestions(self):
        response = self.client.get(self.url, {'author_query': 'mroczna'})
        self.assertEqual(list(response.context['fallback_authors']), [self.pseudonymous])
        self.assertEqual(list(self.client.get(self.url).context['fallback_authors']), [])
        response = self.client.get(reverse('core:author_suggestions'), {'q': '  '})
        self.assertEqual(response.json(), {'results': []})


class ReviewAdminFormTests(TestCase):
    """Formularz recenzji w adminie: powiązanie z istniejącym tekstem i ostrzeżenia o współautorach."""

    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Nabór')
        cls.text = Text.objects.create(title='Opowieść', anthology=cls.book, length=1)
        WorkflowStage.objects.create(text=cls.text, stage_type='ready_for_editing')
        cls.linked = Review.objects.create(anthology=cls.book, title='Opowieść', length=1, copied_text=cls.text,
                                           author_first_name='Ala', author_last_name='Autor', email='ala@example.com')
        cls.review = Review.objects.create(anthology=cls.book, title='Druga', length=1, genre='fantasy',
                                           author_first_name='Ewa', author_last_name='Zakazana', email='ewa@example.com')
        cls.banned = Author.objects.create(first_name='Ewa', last_name='Zakazana', email='ewa@example.com',
                                           is_blacklisted=True)

    def form(self, **changes):
        unbound = ReviewAdminForm(instance=self.review)
        data = {}
        for name, field in unbound.fields.items():
            value = unbound[name].value()
            if isinstance(value, list):
                data[name] = value
            elif value not in (None, False):
                data[name] = field.prepare_value(value) if hasattr(field, 'prepare_value') else value
        data.update(changes)
        form = ReviewAdminForm(data, instance=self.review)
        form.is_valid()
        return form

    def test_editing_other_fields_does_not_repeat_identity_warnings(self):
        """Adres z czarnej listy ostrzega przy zmianie danych autora, a nie przy każdej edycji gatunku."""
        form = self.form(genre='horror')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('czarnej liście', str(self.form(title='Nowy tytuł').errors))

    def test_text_already_linked_needs_confirmation_and_cannot_be_shared(self):
        errors = self.form(copied_text=self.text.pk).errors
        self.assertIn('Sprawdź powiązanie', str(errors['confirm_existing_text']))
        self.assertIn('już powiązany z inną recenzją', str(errors['copied_text']))

    def test_blacklisted_coauthor_requires_confirmation(self):
        form = self.form(coauthors=[self.banned.pk])
        self.assertIn('Współautor', str(form.errors['confirm_submission_warnings']))
        self.assertIn('czarnej liście', str(form.errors['confirm_submission_warnings']))


class ExtractListAndFormTests(TestCase):
    def test_every_search_word_must_match_some_field(self):
        """Wyszukiwanie w Ekstraktach: każde słowo musi pasować do któregoś pola (autor, e-mail, tytuły, nabór)."""
        author = Author.objects.create(first_name='Ola', last_name='Pióro', pseudonym='Sowa', email='ola@example.com')
        Extract.objects.create(author=author, full_name='Ola Pióro', email='ola@example.com', title='Księżyc',
                               recruitment='Ekstrakt 2026')
        Extract.objects.create(author=author, full_name='Ola Pióro', email='ola@example.com', title='Słońce',
                               recruitment='Ekstrakt 2025')
        self.client.force_login(create_superuser())
        url = reverse('core:extract_list')
        for query, titles in (('sowa 2026', ['Księżyc']), ('ola@example.com', ['Księżyc', 'Słońce']),
                              ('sowa brak', [])):
            with self.subTest(query=query):
                page = self.client.get(url, {'q': query}).context['page_obj']
                self.assertEqual(sorted(item.title for item in page), titles)

    def test_add_form_checks_title_decisions_and_dates_then_fills_author_data(self):
        """Dodanie ekstraktu: tytuł przyjęty i odrzucony naraz, decyzja o nienadesłanym tytule i zła data
        nie zapisują rekordu; poprawny wpis bierze imię i e-mail z autora i ustala rodzaj decyzji."""
        author = Author.objects.create(first_name='Ola', last_name='Pióro', email='ola@example.com')
        self.client.force_login(create_superuser())
        url = reverse('core:extract_add')
        self.assertEqual(self.client.get(url).status_code, 200)
        data = {'author': author.pk, 'title': 'Księżyc; Słońce', 'submission_dates': '2026-03-01; 2026-03-04',
                'recruitment': 'Ekstrakt 2027', 'accepted_titles': 'Księżyc', 'rejected_titles': 'Słońce'}
        for changes, field, message in (
            ({'rejected_titles': 'Księżyc'}, 'rejected_titles', 'Ten sam tytuł nie może być przyjęty i odrzucony.'),
            ({'accepted_titles': 'Mars'}, 'accepted_titles', 'Przyjęte tytuły muszą występować na liście nadesłanych.'),
            ({'rejected_titles': 'Mars'}, 'rejected_titles', 'Odrzucone tytuły muszą występować na liście nadesłanych.'),
            ({'submission_dates': 'wczoraj'}, 'submission_dates', ''),
        ):
            with self.subTest(changes=changes):
                response = self.client.post(url, {**data, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, ' '.join(response.context['form'].errors[field]))
                self.assertFalse(Extract.objects.exists())
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse('core:extract_list'), fetch_redirect_response=False)
        extract = Extract.objects.get()
        self.assertEqual((extract.full_name, extract.email, extract.title, extract.status, str(extract.submitted_at)),
                         ('Ola Pióro', 'ola@example.com', 'Księżyc\nSłońce', Extract.Status.MIXED, '2026-03-01'))
