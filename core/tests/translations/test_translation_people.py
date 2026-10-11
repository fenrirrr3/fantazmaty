"""Osoby tłumaczeń: profile autorów zagranicznych i tłumaczy, ich strony, filtry, podpowiedzi i formularz przypisania."""
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.translation_forms import TranslationForm
from fantazmaty.testing import create_superuser, form_value
from texts.admin import TextAdminForm
from texts.models import Anthology, ForeignAuthor, Text, Translator

from .support import CompletedTranslationData, TranslatedBookData


class TranslationProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser('profile-admin')
        cls.book = Anthology.objects.create(title='Przekłady', is_translated=True)
        cls.author = ForeignAuthor.objects.create(first_name='Ursula', last_name='Zagraniczna')
        cls.translator = Translator.objects.create(first_name='Jan', last_name='Tłumaczący', language='angielski')
        cls.text = Text.objects.create(title='Tekst zagraniczny', anthology=cls.book, length=250)
        cls.text.translation.foreign_authors.add(cls.author)
        cls.text.translation.translators.add(cls.translator)

    def setUp(self):
        self.client.force_login(self.user)

    def test_admin_creates_profiles_without_email_and_keeps_domestic_model_separate(self):
        for model in (ForeignAuthor, Translator):
            url = reverse(f'admin:texts_{model._meta.model_name}_add')
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context['adminform'].form.fields['email'].required)
            data = {'first_name': 'Nowa', 'last_name': model.__name__, 'email': '', 'language': 'francuski'}
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(model.objects.filter(first_name='Nowa').exists())
        self.assertFalse(Author.objects.exists())
        form = TextAdminForm(data={'title': 'Nowe tłumaczenie', 'anthology': self.book.pk, 'length': 15,
                                   'current_workflow_cycle': 1})
        self.assertTrue(form.is_valid(), form.errors.as_json())
        text = form.save()
        self.assertFalse(text.authors.exists())
        self.assertIsNotNone(text.translation)

    def test_profile_links_filters_and_visibility(self):
        url = reverse('core:translation_list')
        for params in ({'q': 'angielski'}, {'author': self.author.pk}, {'translator': self.translator.pk}):
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.text.title)
            self.assertContains(response, reverse('core:translation_person_detail', args=['author', self.author.pk]))
            self.assertContains(response, reverse('core:translation_person_detail',
                                                  args=['translator', self.translator.pk]))
        response = self.client.get(reverse('core:translation_person_detail', args=['translator', self.translator.pk]))
        self.assertContains(response, 'angielski')
        self.assertContains(response, self.text.title)
        response = self.client.get(reverse('core:author_list'))
        self.assertNotContains(response, 'Zagraniczna')
        self.assertNotContains(response, 'Tłumaczący')
        response = self.client.get(url)
        nav = html.fromstring(response.content).xpath('//a[contains(@class,"sidebar-link")]/text()')
        nav = [v.strip() for v in nav if v.strip()]
        # Texts group: translations follow the summary table; extracts close the group.
        self.assertEqual(nav.index('Tłumaczenia'), nav.index('Tabelka zbiorcza') + 1)
        self.assertEqual(nav.index('Ekstrakty'), nav.index('Programy') + 1)

    def test_filter_id_spaces_do_not_collide_and_sort_works(self):
        other = Translator.objects.create(first_name='Adam', last_name='Abacki')
        story = Text.objects.create(title='Drugi', anthology=self.book, length=100)
        story.translation.translators.add(other)
        for sort, expected in [('translators', [story.pk, self.text.pk]), ('-translators', [self.text.pk, story.pk])]:
            response = self.client.get(reverse('core:translation_list'), {'sort': sort})
            self.assertEqual([t['pk'] for t in response.context['texts']], expected)
        response = self.client.get(reverse('core:translation_list'), {'translator': other.pk, 'author': self.author.pk})
        self.assertEqual(response.context['page_obj'].paginator.count, 0)


class TranslationPeopleFormTests(TranslatedBookData, TestCase):
    def token(self):
        response = self.client.get(reverse("core:translation_detail", args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        return form_value(response, form_xpath='//form[contains(@action,"tlumacze")]/')

    def test_translation_profiles_form_preserves_author_and_detects_conflict(self):
        endpoint = reverse("core:set_translators", args=[self.text.pk])
        token = self.token()
        response = self.client.post(endpoint, {"foreign_authors": [self.author.pk], "translators": [self.other.pk],
                                               "_edit_version": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.text.translation.translators.all()), [self.other])
        self.assertEqual(list(self.text.translation.foreign_authors.all()), [self.author])
        response = self.client.post(endpoint, {"translators": [self.translator.pk], "_edit_version": token})
        self.assertEqual(response.status_code, 409)
        response = self.client.post(endpoint, {"translators": [999999], "_edit_version": self.token()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.text.translation.translators.all()), [self.other])


class TranslationSuggestionTests(CompletedTranslationData, TestCase):
    def test_autocomplete_permissions_search_and_separate_models(self):
        author_url = reverse('core:translation_person_suggestions', args=['author'])
        translator_url = reverse('core:translation_person_suggestions', args=['translator'])
        self.assertEqual(self.client.get(author_url, {'q': 'Jane'}).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(author_url, {'q': 'Jane'}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(author_url, {'q': 'jane zak'}).json()['results'], [])
        for url, query, expected in ((author_url, 'JZ', self.foreign.pk),
                                     (translator_url, 'francuski', self.translator.pk)):
            response = self.client.get(url, {'q': query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r['id'] for r in response.json()['results']], [expected])
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, 'private@example.test')
        self.assertEqual(self.client.get(author_url, {'q': 'Polska'}).json()['results'], [])
        self.assertEqual(self.client.get(author_url).json()['results'], [])
        invalid = reverse('core:translation_person_suggestions', args=['invalid'])
        self.assertEqual(self.client.get(invalid).status_code, 404)

    def test_form_renders_only_selected_profiles_and_accepts_search_selection(self):
        second = Translator.objects.create(first_name='Nowa', last_name='Osoba')
        record = self.text.translation
        markup = TranslationForm(instance=record).as_p()
        self.assertIn('data-translation-search-url', markup)
        self.assertNotIn('Nowa Osoba', markup)
        self.assertIn('Anna Kowalska', markup)
        data = {'foreign_authors': [self.foreign.pk], 'translators': [self.translator.pk, second.pk],
                'original_verifier': 'Kontroler Przekładu'}
        form = TranslationForm(data=data, instance=record)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(set(record.translators.values_list('pk', flat=True)), {self.translator.pk, second.pk})
        data['translators'] = [second.pk]
        form = TranslationForm(data=data, instance=record)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(list(record.translators.values_list("pk", flat=True)), [second.pk])
        data["translators"] = [99999999]
        self.assertFalse(TranslationForm(data=data, instance=record).is_valid())
