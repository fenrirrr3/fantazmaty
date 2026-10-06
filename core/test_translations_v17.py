from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.translation_forms import TranslationForm
from texts.models import Anthology, Text, ForeignAuthor, Translator
from workflow.completed_import import import_completed_workflow
from workflow.tests import create_member


class TranslationVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('translation-admin', 'a@example.test', 'test')
        cls.member = create_member('translation-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Przekłady v17', is_translated=True)
        cls.normal_book = Anthology.objects.create(title='Polskie v17')
        cls.foreign = ForeignAuthor.objects.create(first_name='Jane', last_name='Żak', pseudonym='JZ')
        cls.domestic = Author.objects.create(pk=cls.foreign.pk, first_name='Polska', last_name='Autorka', email=None)
        cls.translator = Translator.objects.create(first_name='Anna', last_name='Kowalska', language='francuski', email='private@example.test')
        cls.text = Text.objects.create(title='Gotowy przekład v17', anthology=cls.book, length=20)
        cls.text.translation.foreign_authors.add(cls.foreign)
        cls.text.translation.translators.add(cls.translator)
        cls.normal = Text.objects.create(title='Gotowy krajowy v17', anthology=cls.normal_book, length=20)
        cls.normal.authors.add(cls.domestic)
        for text in (cls.text, cls.normal):
            import_completed_workflow(text_id=text.pk, stages=[
                {'stage_type': 'editing', 'assigned_to_id': cls.member.pk},
                {'stage_type': 'editing', 'assigned_to_id': cls.member.pk}], next_stage='ready', preserve_executions=True)

    def test_completed_translation_is_in_personal_all_and_profile_with_executions(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:my_texts'), {'view': 'all'})
        self.assertContains(response, self.text.title)
        self.assertContains(response, self.normal.title)
        self.assertContains(response, 'JZ')
        self.assertNotContains(response, 'Jane Żak')
        row = next(row for row in response.context['texts'] if row['pk'] == self.text.pk)
        self.assertEqual(len(row['user_assignments']), 2)
        self.assertTrue(row['has_completed_work'])
        self.assertFalse(row['has_active_work'] or row['has_reserved_work'])
        response = self.client.get(reverse('core:my_texts'), {'view': 'active'})
        self.assertNotContains(response, self.text.title)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:person_detail', args=[self.member.person_profile.pk]))
        self.assertContains(response, self.text.title)
        self.assertContains(response, reverse('core:translation_person_detail', args=['author', self.foreign.pk]))
        self.assertContains(response, reverse('core:translation_detail', args=[self.text.pk]))
        self.assertEqual(response.context['person_summary']['completed'], 2)

    def test_personal_filters_include_translated_anthology_and_keep_author_ids_separate(self):
        from core.selectors.texts import my_texts_context
        from django.http import QueryDict
        # Give the admin work too so the author filter is available in a personal list.
        from workflow.models import WorkflowRoleAssignment as A
        for text in (self.text, self.normal):
            A.objects.create(text=text, role='proofreader_1', assigned_to=self.admin)
        for value, expected in ((str(self.domestic.pk), self.normal.pk), ('foreign:'+str(self.foreign.pk), self.text.pk)):
            result = my_texts_context(user=self.admin, selected_view='all', params=QueryDict('author='+value))
            self.assertEqual([row['pk'] for row in result['texts']], [expected])
        result = my_texts_context(user=self.admin, selected_view='all', params=QueryDict('anthology='+str(self.book.pk)))
        self.assertEqual([row['pk'] for row in result['texts']], [self.text.pk])
        self.assertIn(self.book.pk, [row['pk'] for row in result['anthologies']])
        result = my_texts_context(user=self.admin, selected_view='all', params=QueryDict('q=Jane'))
        self.assertEqual(list(result['texts']), [])
        result = my_texts_context(user=self.admin, selected_view='all', params=QueryDict('q=JZ'))
        self.assertEqual([row['pk'] for row in result['texts']], [self.text.pk])

    def test_translation_defaults_show_ready_and_aggregate_lists_still_exclude(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:translation_list'))
        self.assertContains(response, self.text.title)
        self.assertFalse(response.context['hide_ready'])
        self.assertFalse(html.fromstring(response.content).xpath('//input[@id="text-hide-ready"]/@checked'))
        response = self.client.get(reverse('core:translation_list'), {'hide_ready': '1'})
        self.assertNotContains(response, self.text.title)
        for route in ('text_list', 'workflow_list'):
            response = self.client.get(reverse('core:'+route), {'hide_ready': '0'})
            self.assertContains(response, self.normal.title)
            self.assertNotContains(response, self.text.title)
            self.assertNotContains(response, self.book.title)

    def test_autocomplete_permissions_search_and_separate_models(self):
        author_url = reverse('core:translation_person_suggestions', args=['author'])
        translator_url = reverse('core:translation_person_suggestions', args=['translator'])
        self.assertEqual(self.client.get(author_url, {'q':'Jane'}).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(author_url, {'q':'Jane'}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(author_url, {'q': 'jane zak'}).json()['results'], [])
        for url, query, expected in ((author_url, 'JZ', self.foreign.pk), (translator_url, 'francuski', self.translator.pk)):
            response = self.client.get(url, {'q':query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r['id'] for r in response.json()['results']], [expected])
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, 'private@example.test')
        self.assertEqual(self.client.get(author_url, {'q':'Polska'}).json()['results'], [])
        self.assertEqual(self.client.get(author_url).json()['results'], [])
        self.assertEqual(self.client.get(reverse('core:translation_person_suggestions', args=['invalid'])).status_code, 404)

    def test_form_renders_only_selected_profiles_and_accepts_search_selection(self):
        second = Translator.objects.create(first_name='Nowa', last_name='Osoba')
        record = self.text.translation
        form = TranslationForm(instance=record)
        self.assertIn('data-translation-search-url', form.as_p())
        self.assertNotIn('Nowa Osoba', form.as_p())
        self.assertIn('Anna Kowalska', form.as_p())
        data = {'foreign_authors':[self.foreign.pk], 'translators':[self.translator.pk, second.pk], 'original_verifier':'Kontroler Przekładu'}
        form = TranslationForm(data=data, instance=record)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(set(record.translators.values_list('pk', flat=True)), {self.translator.pk, second.pk})
        data['translators'] = [second.pk]
        form = TranslationForm(data=data, instance=record)
        self.assertTrue(form.is_valid(), form.errors); form.save()
        self.assertEqual(list(record.translators.values_list('pk', flat=True)), [second.pk])
        data['translators'] = [99999999]
        self.assertFalse(TranslationForm(data=data, instance=record).is_valid())
