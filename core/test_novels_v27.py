from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.selectors.texts import my_texts_context, text_list_context
from texts.models import Anthology, NovelProfile, Text, VocabularyTerm
from texts.novels import new_chapter, edit_token, locked_book, assign_chapters, validate_ready_novel
from texts.vocabulary import canonicalize, merge_plan, merge_token, apply_merge
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.tests import create_member


class NovelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('novel-admin', 'novel-admin@example.test', 'test')
        cls.member = create_member('novel-reader', 'Recenzent')
        cls.editor = create_member('novel-editor', 'Redaktor')
        cls.other = create_member('novel-other', 'Redaktor')
        cls.book = Anthology.objects.create(title='Powieść testowa', is_novel=True)
        cls.profile = cls.book.novel
        cls.author = Author.objects.create(first_name='Ukryte', last_name='Nazwisko', pseudonym='Pseudonim', email='novel-author@example.test')
        cls.profile.authors.add(cls.author)
        cls.profile.tags = 'magia'
        cls.profile.genre = 'fantasy'
        cls.profile.save()
        cls.first = new_chapter(cls.book, cls.profile, title='Rozdział pierwszy', chapter_number=1, length=100)
        cls.second = new_chapter(cls.book, cls.profile, title='Rozdział drugi', chapter_number=2, length=200)
        cls.regular = Anthology.objects.create(title='Zwykła antologia')
        cls.story = Text.objects.create(title='Opowiadanie', anthology=cls.regular, length=123)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('core:novel_detail', args=[self.book.pk])

    def post(self, **data):
        return self.client.post(self.url, {'novel_token': edit_token(self.book, self.admin), **data})

    def batch(self, chapters, assignments, token=None):
        with locked_book(self.book.pk, self.admin, token or edit_token(self.book, self.admin)) as book:
            assign_chapters(book, self.admin, chapters, assignments)

    def test_novel_creation_inherits_authors_metadata_and_pending_work(self):
        self.assertEqual(list(self.first.authors.all()), [self.author])
        self.assertEqual(self.first.tags, 'magia')
        stage = self.first.workflow_stages.get()
        self.assertEqual(stage.stage_type, 'ready_for_editing')
        self.assertIsNone(stage.started_at)
        self.assertFalse(self.first.for_recording)
        response = self.client.post(reverse('core:novel_add'), {'title': 'Nowa', 'authors': [self.author.pk], 'tags': 'kosmos', 'genre': 'science fiction'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NovelProfile.objects.get(anthology__title='Nowa').authors.get(), self.author)

    def test_chapters_hidden_from_general_lists_visible_in_personal_work(self):
        self.batch([self.first.pk], [('editor', self.editor)])
        self.assertNotIn(self.first.pk, text_list_context(user=self.admin, params={})['filtered_queryset'].values_list('pk', flat=True))
        self.assertIn(self.first.pk, [row['pk'] for row in my_texts_context(user=self.editor, selected_view='all')['texts']])
        self.assertNotContains(self.client.get(reverse('core:anthology_list')), self.book.title)
        self.assertNotContains(self.client.get(reverse('core:tag_list')), self.first.title)
        self.assertNotContains(self.client.get(reverse('core:workflow_list')), self.first.title)

    def test_batch_reserves_multiple_chapters_without_starting(self):
        self.batch([self.first.pk, self.second.pk], [('editor', self.editor)])
        self.assertEqual(WorkflowRoleAssignment.objects.filter(text__anthology=self.book, assigned_to=self.editor).count(), 2)
        self.assertFalse(WorkflowStage.objects.filter(text__anthology=self.book, started_at__isnull=False).exists())

    def test_reserved_editor_can_start_existing_workflow(self):
        from core.services.texts import start_assigned_stage
        self.batch([self.first.pk], [('editor', self.editor)])
        start_assigned_stage(user=self.editor, stage_id=self.first.workflow_stages.get().pk, started_at=timezone.localdate())
        self.assertTrue(self.first.workflow_stages.filter(stage_type='editing', started_at__isnull=False).exists())

    def test_multiple_roles_and_existing_verifier_separation(self):
        verifier = create_member('novel-verifier', 'Weryfikator')
        with self.assertRaises(ValidationError):
            self.batch([self.first.pk], [('editor', self.editor), ('verifier_1', verifier), ('verifier_2', verifier)])
        self.assertFalse(self.first.workflow_role_assignments.exists())
        self.batch([self.first.pk], [('editor', self.editor), ('verifier_1', verifier)])
        self.assertEqual(self.first.workflow_role_assignments.count(), 2)

    def test_search_and_intake_keep_books_separate(self):
        from core.views.search import _search_anthologies
        from core.intake_forms import SingleReviewForm
        self.assertEqual(_search_anthologies('Powieść'), [])
        self.assertEqual(_search_anthologies('Powieść', novels=True)[0]['pk'], self.book.pk)
        self.assertFalse(SingleReviewForm().fields['anthology'].queryset.filter(pk=self.book.pk).exists())
        self.assertContains(self.client.get(reverse('core:global_search'), {'query': 'Powieść'}), 'Powieść testowa')

    def test_novel_list_has_pagination_but_chapter_table_does_not(self):
        from lxml import html
        response = self.client.get(self.url, {'page_size': '500'})
        document = html.fromstring(response.content)
        self.assertTrue(document.xpath('//table[@data-pagination="off"]'))
        self.assertIsNone(response.context.get('page_obj'))
        self.assertFalse(document.xpath('//form//form'))
        self.assertContains(self.client.get(reverse('core:novel_list')), '500')

    def test_chapter_admin_form_accepts_inherited_readonly_authors(self):
        from django.contrib import admin
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = self.admin
        form_class = admin.site._registry[Text].get_form(request, self.first)
        form = form_class({'title': self.first.title, 'chapter_number': 1, 'length': 100}, instance=self.first)
        self.assertNotIn('authors', form.fields)
        self.assertTrue(form.is_valid(), form.errors)

    def test_occupied_role_rolls_back_whole_batch_and_history(self):
        self.batch([self.second.pk], [('editor', self.other)])
        with self.assertRaises(ValidationError):
            self.batch([self.first.pk, self.second.pk], [('editor', self.editor)])
        self.assertFalse(self.first.workflow_role_assignments.exists())
        self.assertEqual(self.second.workflow_role_assignments.get().assigned_to, self.other)

    def test_ineligible_assignee_and_outside_chapter_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.batch([self.first.pk], [('editor', self.member)])
        with self.assertRaises(ValidationError):
            self.batch([self.first.pk, self.story.pk], [('editor', self.editor)])
        self.assertFalse(self.first.workflow_role_assignments.exists())

    def test_stale_snapshot_rejects_changes(self):
        token = edit_token(self.book, self.admin)
        self.first.length = 150
        self.first.save(update_fields=['length'])
        with self.assertRaises(ValidationError):
            self.batch([self.first.pk], [('editor', self.editor)], token)

    def test_duplicate_chapter_and_book_type_changes_rejected(self):
        response = self.post(action='chapter', chapter_number=1, title='Inny rozdział', length=300)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.book.texts.count(), 2)
        self.book.is_novel = False
        with self.assertRaises(ValidationError):
            self.book.full_clean()

    def test_form_bulk_and_removed_frontend_metadata(self):
        data = {'action': 'assign', 'chapters': [self.first.pk, self.second.pk],
                'assign-role': 'editor', 'assign-assignee': self.editor.pk}
        self.assertEqual(self.post(**data).status_code, 302)
        self.assertEqual(self.post(action='metadata', title=self.book.title, authors=[self.author.pk], tags='smoki', genre='fantasy').status_code, 400)
        self.first.refresh_from_db()
        self.assertEqual(self.first.tags, 'magia')

    def test_permissions_csrf_and_pseudonyms(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertContains(response, 'Pseudonim')
        self.assertNotContains(response, 'Ukryte Nazwisko')
        self.assertEqual(self.client.post(self.url, {'action': 'chapter'}).status_code, 403)
        self.assertEqual(self.client.get(reverse('core:novel_add')).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url, {'action': 'chapter'}).status_code, 403)

    def test_pages_render_and_chapter_workflow_remains_accessible(self):
        for url in [reverse('core:novel_list'), self.url, reverse('core:novel_add'),
                    reverse('core:chapter_edit', args=[self.book.pk, self.first.pk]),
                    reverse('core:assigned_text_detail', args=[self.first.pk]), reverse('admin:texts_novelprofile_changelist')]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.first.pk]))
        self.assertContains(response, 'Wróć do powieści')

    def test_finishing_requires_completed_chapters_without_extra_approval(self):
        self.assertEqual(self.post(action='finish').status_code, 400)
        self.assertEqual(self.post(action='approve').status_code, 400)
        for chapter in (self.first, self.second):
            chapter.workflow_stages.update(is_current=False)
            WorkflowStage.objects.create(text=chapter, stage_type='ready')
        validate_ready_novel(self.book)
        self.assertEqual(self.post(action='finish').status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'ready')
        self.assertEqual(self.post(action='reopen').status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'in_preparation')


class VocabularyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('dictionary-admin', 'dictionary-admin@example.test', 'test')
        cls.member = create_member('dictionary-reader', 'Recenzent')
        cls.source = VocabularyTerm.objects.create(kind='tag', name='sf')
        cls.target = VocabularyTerm.objects.create(kind='tag', name='science fiction')
        cls.story = Text.objects.create(title='Kosmos', length=10, tags='sf, magia, science fiction', genre='fantasy')

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
        self.assertEqual(self.client.post(reverse('core:vocabulary_list'), {'kind': 'tag', 'name': 'test'}).status_code, 403)
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
