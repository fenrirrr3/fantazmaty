from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from illustrations.models import Illustration
from texts.models import Anthology, Text
from texts.novels import edit_token, new_chapter
from workflow.models import WorkflowRoleAssignment
from workflow.tests import create_member


class ChapterLengthAndListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v37-admin', 'v37-admin@example.test', 'test')
        cls.coordinator = create_member('v37-coordinator', 'Koordynator redakcji')
        cls.member = create_member('v37-editor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Powieść v37', is_novel=True)
        cls.author = Author.objects.create(first_name='Autor', last_name='Powieści', email=None)
        cls.book.novel.authors.add(cls.author)
        cls.chapter = new_chapter(cls.book, cls.book.novel, chapter_number=1, length=123)
        WorkflowRoleAssignment.objects.create(text=cls.chapter, role='editor', assigned_to=cls.member)

    def setUp(self):
        self.client.force_login(self.coordinator)
        self.url = reverse('core:novel_detail', args=[self.book.pk])

    def add(self, numbers, lengths=''):
        return self.client.post(self.url, {'novel_token': edit_token(self.book, self.coordinator),
            'action': 'chapters', 'add-chapter_numbers': numbers, 'add-chapter_lengths': lengths})

    def test_batch_lengths_preserve_existing_work_and_unspecified_lengths(self):
        stage = self.chapter.workflow_stages.get()
        assignment = self.chapter.workflow_role_assignments.get()
        response = self.add('1-4', '1: 999, 2: 12 882\n4: 37435')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.book.texts.order_by('chapter_number').values_list('chapter_number', 'length')),
                         [(1, 123), (2, 12882), (3, None), (4, 37435)])
        self.assertEqual(self.chapter.workflow_stages.get().pk, stage.pk)
        self.assertEqual(self.chapter.workflow_role_assignments.get().pk, assignment.pk)
        self.assertEqual(self.book.texts.get(chapter_number=2).authors.get(), self.author)
        self.assertEqual(self.add('1-4', '2: 1').status_code, 302)
        self.assertEqual(self.book.texts.count(), 4)
        self.assertEqual(self.book.texts.get(chapter_number=2).length, 12882)

    def test_single_length_and_empty_lengths_remain_supported(self):
        self.assertEqual(self.add('2', '15 001').status_code, 302)
        self.assertEqual(self.book.texts.get(chapter_number=2).length, 15001)
        self.assertEqual(self.add('3-4').status_code, 302)
        self.assertTrue(all(value is None for value in self.book.texts.filter(chapter_number__gte=3).values_list('length', flat=True)))

    def test_invalid_lengths_save_no_chapters(self):
        for value in ('500', '2: 0', '2: -4', '2: 1.5', '2: 2147483648', '2: 3, 2: 4', '9: 100', '2: text', '2:3:4'):
            with self.subTest(value=value):
                response = self.add('2-3', value)
                self.assertEqual(response.status_code, 400)
                self.assertIn('chapter_lengths', response.context['chapter_range_form'].errors)
                self.assertEqual(self.book.texts.count(), 1)

    def test_coordinator_can_edit_length_member_cannot_even_with_direct_post(self):
        url = reverse('core:chapter_edit', args=[self.book.pk, self.chapter.pk])
        detail = reverse('core:assigned_text_detail', args=[self.chapter.pk])
        self.assertContains(self.client.get(url), 'name="length"')
        self.assertContains(self.client.get(detail), 'Edytuj rozdział')
        response = self.client.post(url, {'novel_token': edit_token(self.book, self.coordinator), 'chapter_number': 1, 'length': 16000})
        self.assertEqual(response.status_code, 302)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.length, 16000)
        self.client.force_login(self.member)
        response = self.client.get(detail)
        self.assertContains(response, 'Liczba znaków ze spacjami')
        self.assertContains(response, '16000')
        self.assertNotContains(response, 'Edytuj rozdział')
        self.assertNotContains(self.client.get(self.url), 'name="add-chapter_lengths"')
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {'chapter_number': 1, 'length': 1}).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'action': 'chapters', 'add-chapter_numbers': '2', 'add-chapter_lengths': '100'}).status_code, 403)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.length, 16000)
        self.assertEqual(self.book.texts.count(), 1)

    def test_concurrent_change_rejects_chapter_length_edit(self):
        token = edit_token(self.book, self.coordinator)
        self.chapter.length = 17000
        self.chapter.save(update_fields=['length'])
        response = self.client.post(reverse('core:chapter_edit', args=[self.book.pk, self.chapter.pk]),
            {'novel_token': token, 'chapter_number': 1, 'length': 1})
        self.assertEqual(response.status_code, 400)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.length, 17000)

    def test_illustration_columns_removed_only_from_list_with_sorting_intact(self):
        self.client.force_login(self.admin)
        book = Anthology.objects.create(title='Ilustrowana v37', has_illustrations=True)
        text = Text.objects.create(title='Tekst ilustracji', anthology=book, length=100, genre='fantasy', tags='znacznik-tagu')
        art = Illustration.objects.get(text=text)
        art.story_url = 'https://example.test/opowiadanie'
        art.illustrated_excerpt = 'Unikatowy fragment'
        art.trigger_warnings = 'Unikatowe ostrzeżenie'
        art.save()
        response = self.client.get(reverse('illustrations:illustration_list'))
        doc = html.fromstring(response.content)
        table = doc.get_element_by_id('illustrations-table')
        headers = [' '.join(node.itertext()).strip() for node in table.xpath('./thead/tr/th')]
        self.assertEqual(headers, ['Antologia', 'Tytuł tekstu', 'Autorzy', 'Ilustratorzy', 'Status', 'Data przypisania', 'Uwagi koordynatora'])
        self.assertEqual(len(table.xpath('./tbody/tr/td')), 7)
        columns = set(response.context['page_obj'].sort_columns)
        self.assertTrue(set(headers) <= columns)
        self.assertFalse(columns & {'Gatunek, tagi', 'Ostrzeżenia dotyczące treści', 'Opowiadanie', 'Ilustrowany fragment'})
        for value in (art.story_url, art.illustrated_excerpt, art.trigger_warnings, 'znacznik-tagu'):
            self.assertNotContains(response, value)
        detail = self.client.get(reverse('illustrations:illustration_detail', args=[art.pk]))
        for value in (art.story_url, art.illustrated_excerpt, art.trigger_warnings, 'znacznik-tagu'):
            self.assertContains(detail, value)
