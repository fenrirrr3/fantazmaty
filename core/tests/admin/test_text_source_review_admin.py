"""Tworzenie tekstu ze zgłoszenia w adminie: popup autora, potwierdzenia umów i zgodności źródła."""
import time
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.source_reviews import link_source_review
from fantazmaty.testing import create_superuser
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowStage as S

ADD_URL = reverse('admin:texts_text_add')
POPUP_URL = ADD_URL + '?_popup=1&prefill=token'


class TextSourceReviewAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.book = Anthology.objects.create(title='Nabór')
        cls.text = Text.objects.create(title='Tytuł', length=100, anthology=cls.book)

    def setUp(self):
        self.client.force_login(self.admin)

    def review(self, **kw):
        data = dict(title='Tytuł', anthology=self.book, author_first_name='Jan', author_last_name='Autor',
                    email='autor@example.com', length=100, genre='fantasy', status='accepted',
                    author_notified_at=timezone.localdate())
        data.update(kw)
        return Review.objects.create(**data)

    def prefill(self, review, **extra):
        """Zapisuje w sesji dane przygotowane przez przycisk „Utwórz tekst” i zwraca adres popupu."""
        session = self.client.session
        session['review_text_prefills'] = {'token': {
            'user': self.admin.pk, 'at': time.time(), 'review_id': review.pk,
            'data': {'title': review.title, 'length': 100, 'anthology': self.book.pk,
                     'source_author_first_name': review.author_first_name,
                     'source_author_last_name': review.author_last_name,
                     'source_author_email': review.email, **extra},
        }}
        session.save()
        return POPUP_URL

    @staticmethod
    def empty_inlines(response, data):
        for inline in response.context['inline_admin_formsets']:
            data[inline.formset.prefix + '-TOTAL_FORMS'] = '0'
            data[inline.formset.prefix + '-INITIAL_FORMS'] = '0'
        return data

    def popup_data(self, review):
        url = self.prefill(review)
        response = self.client.get(url)
        data = {key: value for key, value in response.context['adminform'].form.initial.items()
                if key != 'source_review_id'}
        data['_save'] = 'Save'
        return url, self.empty_inlines(response, data), response

    def admin_link_form(self, review, text, **overrides):
        from texts.admin import ReviewAdminForm

        data = {name: getattr(review, name) for name in (
            'title', 'genre', 'length', 'content_warnings', 'email', 'phone_number',
            'author_first_name', 'author_last_name', 'author_pseudonym', 'status')}
        data.update(anthology=review.anthology_id, author=review.author_id or '', copied_text=text.pk,
                    confirm_existing_text='on', old_reviews='on')
        data.update(overrides)
        return ReviewAdminForm(data=data, instance=review)

    def test_popup_resolves_new_author_with_contract(self):
        review = self.review(author_pseudonym='Pseudonim autora', phone_number='123456789')
        url = self.prefill(review, source_author_pseudonym=review.author_pseudonym)
        initial = self.client.get(url)
        self.assertEqual(initial.status_code, 200)
        data = self.empty_inlines(initial, {
            'title': review.title, 'length': 100, 'anthology': self.book.pk,
            'source_author_first_name': review.author_first_name,
            'source_author_last_name': review.author_last_name,
            'source_author_email': review.email, 'source_author_pseudonym': review.author_pseudonym,
            'source_contract_received': 'on', 'source_update_author_phone': 'on', '_save': 'Save',
        })
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, response.content[:500])
        review.refresh_from_db()
        self.assertIsNotNone(review.copied_text_id)
        self.assertEqual(review.author.pseudonym, 'Pseudonim autora')
        self.assertEqual(review.author.phone_number, '123456789')
        self.assertTrue(review.author.has_contract)

    def test_admin_and_page_transfer_have_identical_effect(self):
        """Tekst ze zgłoszenia utworzony w adminie i przez copy_review_to_text zostawia ten sam ślad:
        status i powiązania zgłoszenia, autorów, pierwszy etap oraz jeden wpis WorkflowEvent."""
        from core.models import WorkflowEvent
        from core.services.reviews import copy_review_to_text

        coauthor = Author.objects.create(first_name='Anna', last_name='Druga', email='druga@example.com')

        def from_admin(review):
            url, data, _ = self.popup_data(review)
            data.update(source_contract_received='on', source_coauthor_contracts=[str(coauthor.pk)])
            self.assertEqual(self.client.post(url, data).status_code, 200)

        def from_page(review):
            copy_review_to_text(user=self.admin, review_id=review.pk, contract_received=True,
                                confirmed_coauthor_ids=[coauthor.pk])

        results = {}
        for name, transfer, email in (('admin', from_admin, 'a@example.com'), ('strona', from_page, 'b@example.com')):
            with self.subTest(path=name):
                review = self.review(email=email)
                review.coauthors.add(coauthor)
                transfer(review)
                review.refresh_from_db()
                text = review.copied_text
                self.assertIsNotNone(text)
                event = WorkflowEvent.objects.get(text=text)
                results[name] = {
                    'status': review.status, 'author_email': review.author.email,
                    'author_contract': review.author.has_contract, 'detached': review.publication_detached,
                    'authors': set(text.authors.values_list('email', flat=True)) - {email},
                    'stages': list(text.workflow_stages.values_list(
                        'stage_type', 'workflow_cycle', 'iteration', 'is_completed', 'started_at')),
                    'event': (event.previous_status, event.next_status, event.details, event.actor_id),
                }
        self.assertEqual(results['admin'] | {'author_email': 'b@example.com'}, results['strona'])
        self.assertEqual(results['admin']['stages'], [('ready_for_editing', 1, 1, False, None)])
        self.assertEqual(results['admin']['event'][1], 'Do redakcji')

    def test_missing_contract_rolls_back_and_keeps_entered_text(self):
        review = self.review()
        url, data, _ = self.popup_data(review)
        data['title'] = 'Tytuł do zachowania'
        count = Text.objects.count()
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['adminform'].form.non_field_errors())
        self.assertEqual(response.context['adminform'].form['title'].value(), 'Tytuł do zachowania')
        self.assertEqual(Text.objects.count(), count)
        self.assertFalse(Author.objects.filter(email=review.email).exists())
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)

    def test_popup_confirms_coauthor_contracts(self):
        review = self.review()
        coauthor = Author.objects.create(first_name='Anna', last_name='Druga', email='druga@example.com')
        review.coauthors.add(coauthor)
        url, data, response = self.popup_data(review)
        self.assertIn(coauthor, response.context['adminform'].form.fields['source_coauthor_contracts'].queryset)
        data['source_contract_received'] = 'on'
        data['source_coauthor_contracts'] = [str(coauthor.pk)]
        response = self.client.post(url, data)
        self.assertIn(response.status_code, (200, 302))
        review.refresh_from_db()
        coauthor.refresh_from_db()
        self.assertTrue(coauthor.has_contract)
        self.assertEqual(set(review.copied_text.authors.values_list('pk', flat=True)),
                         {review.author_id, coauthor.pk})

    def test_popup_authors_are_readonly_and_forged_selection_ignored(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor@example.com',
                                       has_contract=True)
        other = Author.objects.create(first_name='Inna', last_name='Osoba', email='inna@example.com',
                                      has_contract=True)
        review = self.review(author=author)
        url, data, response = self.popup_data(review)
        self.assertTrue(response.context['adminform'].form.fields['authors'].disabled)
        data['authors'] = [str(other.pk)]
        self.assertIn(self.client.post(url, data).status_code, (200, 302))
        review.refresh_from_db()
        self.assertEqual(list(review.copied_text.authors.all()), [author])

    def test_popup_rejects_unsaved_data_and_stale_snapshot(self):
        review = self.review()
        endpoint = reverse('admin:texts_review_prepare_text')
        response = self.client.post(endpoint, {'review_id': review.pk, 'title': 'Niezapisany tytuł'})
        self.assertEqual(response.status_code, 409)
        response = self.client.post(endpoint, {'review_id': review.pk})
        self.assertEqual(response.status_code, 200)
        url = response.json()['url']
        response = self.client.get(url)
        data = {k: v for k, v in response.context['adminform'].form.initial.items() if k != 'source_review_id'}
        data.update(source_contract_received='on', _save='Save')
        self.empty_inlines(response, data)
        review.title = 'Zapisany później tytuł'
        review.save()
        result = self.client.post(url, data)
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, 'Recenzja zmieniła się')
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)
        self.assertFalse(Author.objects.filter(email=review.email).exists())

    def test_late_popup_error_rolls_back_and_renders_form(self):
        """Chroni przed częściowym zapisem tekstu i autora, gdy błąd pojawia się po zapisie formularza."""
        from texts.admin import TextAdmin

        review = self.review()
        url, data, _ = self.popup_data(review)
        data['source_contract_received'] = 'on'
        count = Text.objects.count()
        original = TextAdmin.save_related

        def fail_after_save(admin, request, form, formsets, change):
            original(admin, request, form, formsets, change)
            raise ValidationError('Zapis został przerwany – sprawdź dane.')

        with patch.object(TextAdmin, 'save_related', fail_after_save):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Zapis został przerwany')
        self.assertEqual(Text.objects.count(), count)
        self.assertFalse(Author.objects.filter(email=review.email).exists())
        review.refresh_from_db()
        self.assertIsNone(review.author_id)
        self.assertIsNone(review.copied_text_id)

    def test_direct_popup_uses_publication_gates_and_plain_add_has_no_confirmations(self):
        self.assertNotContains(self.client.get(ADD_URL), 'id_source_contract_received')
        review = self.review()
        response = self.client.get(ADD_URL, {'source_review': review.pk, '_popup': 1})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id_source_contract_received')
        review.status = 'rejected'
        review.save()
        self.assertEqual(self.client.get(ADD_URL, {'source_review': review.pk, '_popup': 1}).status_code, 400)

    def test_same_names_different_profiles_require_confirmation(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor@example.com')
        other = Author.objects.create(first_name='Jan', last_name='Autor', email='inny@example.com')
        self.text.authors.add(author)
        review = self.review(author=other)
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=review.pk)

    def test_review_form_allows_cross_anthology_but_rejects_unaccepted_source(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor@example.com')
        self.text.authors.add(author)
        S.objects.create(text=self.text, stage_type='editing')
        other = Anthology.objects.create(title='Inny nabór')
        review = self.review(author=author, old_reviews=False, anthology=other)
        form = self.admin_link_form(review, self.text, confirm_source_mismatch='on', old_reviews='')
        self.assertTrue(form.is_valid(), form.errors)
        for changes in ({'status': 'new'}, {'status': 'in_review'}):
            review = self.review(author=author, old_reviews=False, **changes)
            form = self.admin_link_form(review, self.text, confirm_source_mismatch='on', old_reviews='')
            self.assertFalse(form.is_valid())
            self.assertIn('copied_text', form.errors)
            self.assertIn('Wybierz przyjęte zgłoszenie', str(form.errors['copied_text']))

    def test_review_form_requires_separate_confirmation_of_author_mismatch(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor@example.com')
        other = Author.objects.create(first_name='Inna', last_name='Osoba', email='inna@example.com')
        self.text.authors.add(author)
        S.objects.create(text=self.text, stage_type='editing')
        review = self.review(author=other, old_reviews=True)
        form = self.admin_link_form(review, self.text)
        self.assertFalse(form.is_valid())
        self.assertIn('rozbieżności', str(form.errors['copied_text']))
        form = self.admin_link_form(review, self.text, confirm_source_mismatch='on')
        form.is_valid()
        self.assertNotIn('copied_text', form.errors)
