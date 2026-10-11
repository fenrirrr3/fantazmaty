"""Rozdziały powieści: dodawanie zakresami, długości, numeracja, zbiorcze przypisania, praca na rozdziale i panele strony rozdziału."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.novel_forms import ChapterForm, ChapterRangeForm, NovelForm
from core.selectors.texts import my_texts_context
from core.services.texts import start_assigned_stage
from core.tag_forms import TextTagsForm
from fantazmaty.testing import create_superuser
from illustrations.models import Illustrator
from texts.models import Anthology, NovelProfile, Text, VocabularyTerm
from texts.novels import edit_token, new_chapter, parse_chapter_numbers
from workflow.models import WorkflowRoleAssignment
from workflow.services import send_to_first_verification

from .support import NovelData


class NovelChapterTests(NovelData, TestCase):
    def post(self, user=None, **data):
        return self.client.post(self.url, {'novel_token': edit_token(self.book, user or self.admin), **data})

    def assignment(self, **extra):
        return {'action': 'assign', 'assign-role': 'editor', 'assign-assignee': self.editor.pk, **extra}

    def test_mixed_tag_separators_and_aliases(self):
        target = VocabularyTerm.objects.create(kind='tag', name='science fiction')
        VocabularyTerm.objects.create(kind='tag', name='sf', canonical=target)
        value = 'magia, SF\nmagia\r\nwojna, science fiction'
        form = TextTagsForm({'tags': value, 'genre': ''}, instance=self.chapter)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['tags'], 'magia, science fiction, wojna')
        form = NovelForm({'title': 'Powieść', 'authors': [self.author.pk], 'tags': value})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['tags'], 'magia, science fiction, wojna')

    def test_create_novel_with_new_author_without_email(self):
        response = self.client.post(reverse('core:novel_add'), {'title': 'Nowa powieść',
            'new_author_first_name': 'Anna', 'new_author_last_name': 'Nowak', 'new_author_pseudonym': 'Gwiazda'})
        self.assertEqual(response.status_code, 302)
        book = Anthology.objects.get(title='Nowa powieść')
        author = book.novel.authors.get()
        self.assertIsNone(author.email)
        self.assertEqual(author.display_name, 'Gwiazda')
        self.assertEqual(list(new_chapter(book, book.novel, chapter_number=1).authors.all()), [author])

    def test_invalid_or_duplicate_author_does_not_create_novel(self):
        for extra in ({}, {'new_author_first_name': 'Jan'},
                      {'new_author_first_name': 'Jan', 'new_author_last_name': 'Kowalski', 'new_author_pseudonym': 'Pióro'},
                      {'new_author_first_name': 'Jan', 'new_author_last_name': 'Test', 'new_author_email': 'wrong'}):
            with self.subTest(extra=extra):
                response = self.client.post(reverse('core:novel_add'), {'title': 'Nie powstanie', **extra})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(Anthology.objects.filter(title='Nie powstanie').exists())
        author = Author.objects.create(first_name='Inny', last_name='Autor', email='duplicate@example.test')
        response = self.client.post(reverse('core:novel_add'), {'title': 'Nie powstanie',
            'new_author_first_name': 'Anna', 'new_author_last_name': 'Nowak', 'new_author_email': author.email.upper()})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Anthology.objects.filter(title='Nie powstanie').exists())

    def test_create_ranges_skip_existing_and_keep_workflow(self):
        stage_id = self.chapter.workflow_stages.get().pk
        response = self.post(action='chapters', **{'add-chapter_numbers': '1-2, 4–7, 4'})
        self.assertEqual(response.status_code, 302)
        chapters = self.book.texts.order_by('chapter_number')
        self.assertEqual(list(chapters.values_list('chapter_number', flat=True)), [1, 2, 4, 5, 6, 7])
        self.assertEqual(self.chapter.workflow_stages.get().pk, stage_id)
        for chapter in chapters:
            self.assertEqual(chapter.title, f'Rozdział {chapter.chapter_number}')
            self.assertIsNone(chapter.length)
            self.assertEqual(chapter.authors.get(), self.author)
            self.assertEqual(chapter.workflow_stages.get().stage_type, 'ready_for_editing')

    def test_invalid_ranges_write_nothing(self):
        for value in ('', '0', '7-4', '1,,2', '1-501', '1-400,401-600', '1-2,abc', '-1', '2147483648', '1-2;4'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_chapter_numbers(value)
        self.assertEqual(self.post(action='chapters', **{'add-chapter_numbers': '2, 0'}).status_code, 400)
        self.assertEqual(self.book.texts.count(), 1)

    def test_assignment_spans_all_visible_chapters_and_keeps_missing_numbers(self):
        self.post(action='chapters', **{'add-chapter_numbers': '2-27'})
        response = self.client.get(self.url, {'page_size': 25})
        self.assertIn(26, [row['chapter'].chapter_number for row in response.context['rows']])
        self.assertEqual(self.post(**self.assignment(chapters=list(self.book.texts.filter(chapter_number__in=[1, 2, 26, 27]).values_list('pk', flat=True)))).status_code, 302)
        for chapter in self.book.texts.filter(chapter_number__in=[1, 2, 26, 27]):
            self.assertEqual(chapter.workflow_role_assignments.get().assigned_to, self.editor)
            self.assertIsNone(chapter.workflow_stages.get().started_at)
        self.assertEqual(self.book.texts.count(), 27)
        self.assertFalse(self.book.texts.filter(chapter_number__range=(3, 25), workflow_role_assignments__isnull=False).exists())

    def test_missing_range_or_occupied_role_rolls_back_all(self):
        self.assertEqual(self.post(**self.assignment(chapters=[self.chapter.pk, 999999])).status_code, 400)
        self.assertFalse(self.chapter.workflow_role_assignments.exists())
        second = new_chapter(self.book, self.book.novel, chapter_number=2)
        data = self.assignment(chapters=[second.pk])
        data['assign-assignee'] = self.other.pk
        self.assertEqual(self.post(**data).status_code, 302)
        self.assertEqual(self.post(**self.assignment(chapters=[self.chapter.pk, second.pk])).status_code, 400)
        self.assertFalse(self.chapter.workflow_role_assignments.exists())
        self.assertEqual(second.workflow_role_assignments.get().assigned_to, self.other)

    def test_number_only_form_and_member_workflow(self):
        self.assertEqual(set(ChapterForm().fields), {'chapter_number', 'length'})
        self.assertEqual(self.post(**self.assignment(chapters=[self.chapter.pk])).status_code, 302)
        self.assertIn(self.chapter.pk, [row['pk'] for row in my_texts_context(user=self.editor, selected_view='all')['texts']])
        start_assigned_stage(user=self.editor, stage_id=self.chapter.workflow_stages.get().pk, started_at=timezone.localdate())
        self.assertTrue(self.chapter.workflow_stages.filter(stage_type='editing', started_at__isnull=False).exists())
        self.client.force_login(self.editor)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.chapter.pk]))
        self.assertContains(response, 'Przekaż do pierwszej weryfikacji')
        self.assertNotContains(response, '<dt>Długość</dt>', html=True)
        self.assertNotContains(response, 'name="content_warnings"')
        self.assertNotContains(response, 'name="file_url"')
        send_to_first_verification(self.chapter, self.editor)
        self.assertTrue(self.chapter.workflow_stages.filter(stage_type='editing', is_completed=True, ended_at__isnull=False).exists())
        self.assertTrue(self.chapter.workflow_stages.filter(stage_type='first_verification', is_completed=False).exists())

    def test_tasks_hidden_everywhere_from_regular_member(self):
        for user, visible in ((self.admin, True), (self.coordinator, True), (self.editor, False)):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertEqual('Zadania całej powieści' in response.content.decode(), visible)
            response = self.client.get(reverse('core:task_list'))
            self.assertEqual(self.book.title in response.content.decode(), visible)
        self.assertEqual(self.post(user=self.editor, action='production').status_code, 403)
        self.assertEqual(self.post(user=self.editor, action='chapters', **{'add-chapter_numbers': '2'}).status_code, 403)

    def test_chapter_renumber_and_admin_have_no_metadata_fields(self):
        response = self.client.post(reverse('core:chapter_edit', args=[self.book.pk, self.chapter.pk]),
            {'chapter_number': '9', 'novel_token': edit_token(self.book, self.admin)})
        self.assertEqual(response.status_code, 302)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.title, 'Rozdział 9')
        response = self.client.get(reverse('admin:texts_text_change', args=[self.chapter.pk]))
        document = html.fromstring(response.content)
        for field in ('title', 'file_url', 'content_warnings'):
            self.assertFalse(document.xpath(f'//input[@name="{field}"]|//textarea[@name="{field}"]'))
        with self.assertRaises(ValidationError):
            Text(title='Zwykły tekst').full_clean()

    def test_numbering_preserves_work_and_old_metadata(self):
        Text.objects.filter(pk=self.chapter.pk).update(title='Dawny tytuł', length=123, file_url='https://example.test/', content_warnings='wojna')
        self.chapter.refresh_from_db()
        self.chapter.save()
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.title, 'Rozdział 1')
        self.assertEqual(self.chapter.length, 123)
        self.assertEqual(self.chapter.file_url, 'https://example.test/')
        self.assertEqual(self.chapter.content_warnings, 'wojna')
        self.assertEqual(self.chapter.workflow_stages.count(), 1)

    def test_forms_valid_html_and_invalid_selection_survives(self):
        response = self.post(**self.assignment(chapters=[self.chapter.pk, 999999]))
        self.assertEqual(response.status_code, 400)
        document = html.fromstring(response.content)
        self.assertEqual(document.xpath('//input[@name="chapters"][@checked]/@value'), [str(self.chapter.pk)])
        self.assertFalse(document.xpath('//form//form'))
        self.assertContains(response, 'Wybierz rozdziały należące do tej powieści.', status_code=400)


class ChapterLengthTests(NovelData, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chapter.length = 123
        cls.chapter.save(update_fields=['length'])
        WorkflowRoleAssignment.objects.create(text=cls.chapter, role='editor', assigned_to=cls.editor)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.coordinator)

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
                form = ChapterRangeForm({'chapter_numbers': '2-3', 'chapter_lengths': value})
                self.assertFalse(form.is_valid())
                self.assertIn('chapter_lengths', form.errors)
        response = self.add('2-3', '2: 3, 2: 4')
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
        self.client.force_login(self.editor)
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

    def test_chapter_length_optional_and_must_be_positive(self):
        self.client.force_login(self.admin)
        url = reverse('core:chapter_edit', args=[self.book.pk, self.chapter.pk])
        for length, expected in [('1500', 1500), ('', None)]:
            response = self.client.post(url, {'novel_token': edit_token(self.book, self.admin),
                                              'chapter_number': 1, 'length': length})
            self.assertEqual(response.status_code, 302)
            self.chapter.refresh_from_db()
            self.assertEqual(self.chapter.length, expected)
        response = self.client.post(url, {'novel_token': edit_token(self.book, self.admin), 'chapter_number': 1,
                                          'length': 0})
        self.assertEqual(response.status_code, 400)


class TextDetailPanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('ui34')
        cls.active = Illustrator.objects.create(first_name='Anna', last_name='Aktywna')
        cls.inactive = Illustrator.objects.create(first_name='Jan', last_name='Nieaktywny', is_active=False)
        cls.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.story = Text.objects.create(title='Tekst', anthology=cls.book, length=100)
        cls.novel = Anthology.objects.create(title='Powieść', is_novel=True)
        profile, _ = NovelProfile.objects.get_or_create(anthology=cls.novel)
        cls.chapter = new_chapter(cls.novel, profile, chapter_number=1)

    def setUp(self):
        activity = patch('core.activity_spool.enqueue_activity')
        activity.start()
        self.addCleanup(activity.stop)
        self.client.force_login(self.admin)


    def test_story_and_chapter_have_paired_collapsed_panels(self):
        for story in (self.story, self.chapter):
            with self.subTest(story=story.title):
                response = self.client.get(reverse('core:assigned_text_detail', args=[story.pk]))
                self.assertEqual(response.status_code, 200)
                doc = html.fromstring(response.content)
                for pair, expected in [('credits-history', ['text-credits', 'text-stage-history']),
                                       ('management', ['text-withdraw', 'text-handoff'])]:
                    panels = doc.xpath(f'//div[@data-detail-pair="{pair}"]/details')
                    self.assertEqual([panel.get('id') for panel in panels], expected)
                    self.assertTrue(all('open' not in panel.attrib for panel in panels))
                self.assertNotContains(response, 'Zarządzanie tekstem')
                self.assertEqual(doc.xpath('normalize-space(//*[@id="text-withdraw"]/summary/span[1])'), 'Wycofaj tekst')
                self.assertEqual(doc.xpath('normalize-space(//*[@id="text-credits"]/summary/span[1])'), 'Stopka tekstu')
                if story == self.chapter:
                    self.assertFalse(doc.xpath('//*[@id="text-audiobook"]'))
