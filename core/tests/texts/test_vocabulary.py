"""Słownik tagów i gatunków: scalanie haseł, uprawnienia i podpowiedzi, migracja zasilająca oraz
strona słownika z dwiema niezależnymi kolumnami."""
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from lxml import html

from fantazmaty.testing import create_member, create_superuser
from texts.models import Text, VocabularyTerm
from texts.vocabulary import apply_merge, canonicalize, merge_plan, merge_token


class VocabularyMergeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('dictionary-admin')
        cls.member = create_member('dictionary-reader', 'Recenzent')
        cls.source = VocabularyTerm.objects.create(kind='tag', name='sf')
        cls.target = VocabularyTerm.objects.create(kind='tag', name='science fiction')
        cls.story = Text.objects.create(title='Kosmos', length=10, tags='sf, magia, science fiction',
                                        genre='fantasy')

    def setUp(self):
        self.client.force_login(self.admin)

    def plan(self):
        changes, digest = merge_plan(self.source, self.target)
        return changes, merge_token(self.source, self.target, self.admin, digest)

    def test_preview_then_merge_preserves_other_fields_and_registers_alias(self):
        changes, token = self.plan()
        self.assertEqual(changes[0]['after'], 'science fiction, magia')
        self.story.refresh_from_db()
        self.assertEqual(self.story.tags, 'sf, magia, science fiction')
        self.assertEqual(apply_merge(self.source.pk, self.target.pk, self.admin, token), 1)
        self.story.refresh_from_db()
        self.assertEqual(self.story.genre, 'fantasy')
        self.assertEqual(self.story.tags, 'science fiction, magia')
        self.assertEqual(canonicalize('SF, science fiction', 'tag'), 'science fiction')

    def test_stale_preview_and_invalid_target_are_atomic(self):
        _, token = self.plan()
        self.story.tags = 'sf, nowy'
        self.story.save(update_fields=['tags'])
        with self.assertRaises(ValidationError):
            apply_merge(self.source.pk, self.target.pk, self.admin, token)
        self.source.refresh_from_db()
        self.assertIsNone(self.source.canonical_id)
        genre = VocabularyTerm.objects.create(kind='genre', name='fantasy')
        with self.assertRaises(ValidationError):
            merge_plan(self.source, genre)

    def test_genre_overflow_does_not_mutate_any_record(self):
        source = VocabularyTerm.objects.create(kind='genre', name='sf')
        target = VocabularyTerm.objects.create(kind='genre', name='x' * 100)
        self.story.genre = 'sf, horror'
        self.story.save(update_fields=['genre'])
        with self.assertRaises(ValidationError):
            merge_plan(source, target)
        self.story.refresh_from_db()
        self.assertEqual(self.story.genre, 'sf, horror')

    def test_vocabulary_pages_permissions_and_suggestions(self):
        self.assertEqual(self.client.get(reverse('core:vocabulary_list')).status_code, 200)
        url = reverse('core:vocabulary_merge', args=[self.source.pk])
        response = self.client.get(url, {'target': self.target.pk})
        self.assertContains(response, 'Kosmos')
        self.assertContains(response, 'Zatwierdź scalenie')
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        response = self.client.post(reverse('core:vocabulary_list'), {'kind': 'tag', 'name': 'test'})
        self.assertEqual(response.status_code, 403)
        response = self.client.get(reverse('core:vocabulary_suggestions'), {'kind': 'tag', 'q': 'science'})
        self.assertEqual(response.json()['results'], ['science fiction'])

    def test_seed_preserves_original_fields_and_distinguishes_polish_letters(self):
        self.story.tags = 'Łódź, ŁÓDŹ, Lodz'
        self.story.save(update_fields=['tags'])
        module = import_module('texts.migrations.0028_seed_vocabulary')
        module.seed(apps, SimpleNamespace(connection=connection))
        self.assertEqual(VocabularyTerm.objects.filter(name__in=['Łódź', 'ŁÓDŹ', 'Lodz']).count(), 2)
        self.story.refresh_from_db()
        self.assertEqual(self.story.tags, 'Łódź, ŁÓDŹ, Lodz')


class VocabularyPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('dictionary58')
        for kind in ('genre', 'tag'):
            for i in range(61):
                VocabularyTerm.objects.create(kind=kind, name=f'{kind} {i:02}')

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('core:vocabulary_list')

    def test_pages_sorting_and_sizes_are_independent_and_preserved(self):
        response = self.client.get(self.url, {'genre_page': 2, 'genre_size': 25, 'genre_sort': '-name',
                                              'tag_page': 2, 'tag_size': 50, 'tag_sort': 'name'})
        self.assertEqual(response.status_code, 200)
        genre, tag = response.context['sections']
        self.assertEqual((genre['page'].number, tag['page'].number), (2, 2))
        self.assertEqual((genre['page'][0].name, tag['page'][0].name), ('genre 35', 'tag 50'))
        params = parse_qs(urlsplit(genre['page'].first_url).query)
        self.assertEqual(params['tag_page'], ['2'])
        self.assertEqual(params['tag_sort'], ['name'])
        doc = html.fromstring(response.content)
        self.assertEqual(doc.xpath('//nav[@data-sort-config]/@data-sort-param'), ['genre_sort', 'tag_sort'])
        self.assertFalse(doc.xpath('//form//form'))
        response = self.client.get(self.url)
        self.assertEqual([s['page'].paginator.per_page for s in response.context['sections']], [25, 50])

    def test_search_and_clear_do_not_reset_other_column(self):
        response = self.client.get(self.url, {'genre_q': 'genre 00', 'tag_q': 'tag 1', 'tag_page': 1})
        genre, tag = response.context['sections']
        self.assertEqual(genre['page'].paginator.count, 1)
        self.assertEqual(tag['page'].paginator.count, 10)
        params = parse_qs(urlsplit(genre['clear_url']).query)
        self.assertEqual(params['tag_q'], ['tag 1'])
        self.assertNotIn('genre_q', params)
        self.assertIn(('tag_q', 'tag 1'), genre['preserved'])

    def test_separate_add_forms_above_tables_save_kind_and_keep_errors_in_correct_column(self):
        doc = html.fromstring(self.client.get(self.url).content)
        self.assertEqual(doc.xpath('//input[@name="kind"]/@value'), ['genre', 'tag'])
        for section in doc.xpath('//section[contains(@class,"vocabulary-column")]'):
            add = section.xpath('.//div[contains(@class,"vocabulary-add")]')[0]
            table = section.xpath('.//table')[0]
            self.assertLess(list(section.iter()).index(add), list(section.iter()).index(table))
        for kind in ('genre', 'tag'):
            self.assertEqual(self.client.post(self.url, {'kind': kind, 'name': 'Nowe hasło'}).status_code, 302)
        self.assertEqual(VocabularyTerm.objects.filter(name='Nowe hasło').count(), 2)
        response = self.client.post(self.url, {'kind': 'genre', 'name': ''})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['sections'][0]['form'].errors)
        self.assertFalse(response.context['sections'][1]['form'].errors)
