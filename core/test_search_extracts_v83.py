from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from texts.models import Anthology, Extract, Text
from workflow.tests import create_member


class ExtractSearchTests(TestCase):
    def setUp(self):
        self.manager = create_member('extract-search-manager', 'Koordynator redakcji')
        self.member = create_member('extract-search-member', 'Redaktor')
        self.author = Author.objects.create(first_name='Anna', last_name='Żurawska', email='author@example.test')
        self.entry = Extract.objects.create(
            author=self.author, full_name='Stary zapis', email='old@example.test',
            recruitment='Ekstrakty 2', title='Mała podróż;Inny tekst;Jeszcze jeden',
            accepted_titles='Mała podróż', rejected_titles='Inny tekst',
        )
        self.url = reverse('core:global_search')
        self.client.force_login(self.manager)

    def test_author_search_finds_extract_titles_and_preserves_decisions_and_links(self):
        response = self.client.get(self.url, {'q': 'anna zurawska'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([entry['pk'] for entry in response.context['extracts']], [self.entry.pk])
        self.assertEqual([item['status'] for item in response.context['extracts'][0]['titles']],
                         ['Przyjęty', 'Odrzucony', 'Bez decyzji'])
        for value in ('Mała podróż', 'Inny tekst', 'Jeszcze jeden', 'Ekstrakty 2'):
            self.assertContains(response, value)
        self.assertNotContains(response, 'Stary zapis')
        self.assertContains(response, reverse('core:extract_edit', args=[self.entry.pk]))
        self.assertEqual(self.client.get(reverse('core:extract_edit', args=[self.entry.pk])).status_code, 200)
        document = html.fromstring(response.content)
        self.assertEqual(document.xpath('//div[@class="global-search-results"]/section/header/h2/text()'),
                         ['Ekstrakty', 'Autorzy'])

    def test_title_anthology_and_current_pseudonym_search_without_old_name_leaks(self):
        self.author.pseudonym = 'Pióro Nocy'
        self.author.save()
        for query in ('Pioro Nocy', 'mała podróż', 'Ekstrakty 2', 'author@example.test'):
            with self.subTest(query=query):
                response = self.client.get(self.url, {'q': query})
                self.assertEqual(response.context['extracts'].total, 1)
                section = html.fromstring(response.content).xpath('//section[@aria-labelledby="search-extracts-heading"]')[0]
                self.assertIn('Pióro Nocy', section.text_content())
                self.assertNotIn('Żurawska', section.text_content())
                self.assertNotIn('Stary zapis', section.text_content())
        for query in ('Stary zapis', 'Anna Żurawska'):
            self.assertFalse(self.client.get(self.url, {'q': query}).context['extracts'])

    def test_members_cannot_discover_restricted_extract_submissions(self):
        self.client.force_login(self.member)
        for query in ('Anna', 'Mała podróż', 'Ekstrakty 2'):
            response = self.client.get(self.url, {'q': query})
            self.assertFalse(response.context['extracts'])
            self.assertNotContains(response, 'search-extracts-heading')
            self.assertNotContains(response, reverse('core:extract_edit', args=[self.entry.pk]))

    def test_empty_categories_are_hidden_and_no_matches_has_one_message(self):
        book = Anthology.objects.create(title='Zbiór')
        Text.objects.create(title='Samotny wynik', anthology=book, length=1)
        response = self.client.get(self.url, {'q': 'Samotny wynik'})
        document = html.fromstring(response.content)
        self.assertEqual(document.xpath('//div[@class="global-search-results"]/section/header/h2/text()'), ['Teksty'])
        self.assertNotContains(response, 'Brak wyników dla podanego zapytania.')
        response = self.client.get(self.url, {'q': 'Niematakwpisanegorekordu'})
        document = html.fromstring(response.content)
        self.assertFalse(document.xpath('//div[@class="global-search-results"]/section'))
        self.assertContains(response, 'Brak wyników dla podanego zapytania.', count=1)
        self.assertNotContains(self.client.get(self.url), 'Brak wyników dla podanego zapytania.')

    def test_extract_results_have_independent_pagination(self):
        for number in range(15):
            Extract.objects.create(author=self.author, full_name='Anna Żurawska', email=self.author.email,
                title=f'Tekst {number}', recruitment=f'Ekstrakty test {number:02}')
        response = self.client.get(self.url, {'q': 'Anna Żurawska'})
        results = response.context['extracts']
        self.assertEqual(results.total, 16)
        self.assertEqual(len(results), 15)
        self.assertIn('extracts_page=2', results.next_url)
        second = self.client.get(self.url + results.next_url)
        self.assertEqual(len(second.context['extracts']), 1)
        self.assertEqual(second.context['authors'].page.number, 1)
        self.assertEqual(second.context['extracts'].page.number, 2)
        self.assertContains(second, 'search-extracts-heading')
        self.assertEqual(self.client.get(self.url, {'q': 'Anna', 'extracts_page': 'oops'}).status_code, 200)
