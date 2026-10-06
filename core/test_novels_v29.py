from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.novel_forms import NovelForm, ChapterForm
from core.tag_forms import TextTagsForm
from texts.models import Anthology, Text, VocabularyTerm
from texts.novels import edit_token, new_chapter, parse_chapter_numbers
from workflow.tests import create_member


class NovelChangesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('book-admin', 'book-admin@example.test', 'test')
        cls.coordinator = create_member('book-coordinator', 'Koordynator redakcji')
        cls.editor = create_member('book-editor', 'Redaktor')
        cls.other = create_member('book-editor-other', 'Redaktor')
        cls.book = Anthology.objects.create(title='Książka z rozdziałami', is_novel=True)
        cls.author = Author.objects.create(first_name='Jan', last_name='Kowalski', pseudonym='Pióro', email=None)
        cls.book.novel.authors.add(cls.author)
        cls.chapter = new_chapter(cls.book, cls.book.novel, chapter_number=1)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('core:novel_detail', args=[self.book.pk])

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
        for value in ('', '0', '7-4', '1,,2', '1-501', '1-2,abc', '-1', '2147483648', '1-2;4'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_chapter_numbers(value)
        self.assertEqual(self.post(action='chapters', **{'add-chapter_numbers': '2, 0'}).status_code, 400)
        self.assertEqual(self.book.texts.count(), 1)

    def test_assignment_range_spans_pages_and_keeps_missing_numbers(self):
        self.post(action='chapters', **{'add-chapter_numbers': '2-27'})
        response = self.client.get(self.url, {'page_size': 25})
        self.assertNotIn(26, [row['chapter'].chapter_number for row in response.context['rows']])
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
        from core.services.texts import start_assigned_stage
        from core.selectors.texts import my_texts_context
        self.assertIn(self.chapter.pk, [row['pk'] for row in my_texts_context(user=self.editor, selected_view='all')['texts']])
        start_assigned_stage(user=self.editor, stage_id=self.chapter.workflow_stages.get().pk, started_at=timezone.localdate())
        self.assertTrue(self.chapter.workflow_stages.filter(stage_type='editing', started_at__isnull=False).exists())
        self.client.force_login(self.editor)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.chapter.pk]))
        self.assertContains(response, 'Przekaż do pierwszej weryfikacji')
        self.assertNotContains(response, '<dt>Długość</dt>', html=True)
        self.assertNotContains(response, 'name="content_warnings"')
        self.assertNotContains(response, 'name="file_url"')
        from workflow.services import send_to_first_verification
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
