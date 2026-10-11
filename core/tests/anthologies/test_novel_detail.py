"""Powieści: tworzenie, ukrycie rozdziałów w listach ogólnych, uprawnienia, metadane w adminie, układ strony, brak produkcji i zakończenie pracy."""
from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.intake_forms import SingleReviewForm
from core.selectors.texts import my_texts_context, text_list_context
from core.views.search import _search_anthologies
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustration
from illustrations.services import sync_required_illustrations
from texts.models import Anthology, NovelProfile, Text
from texts.novels import add_chapters, assign_chapters, edit_token, locked_book, new_chapter, validate_ready_novel
from workflow.models import WorkflowRoleAssignment, WorkflowStage

from .support import NovelData


class NovelLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('novel-admin')
        cls.member = create_member('novel-reader', 'Recenzent')
        cls.editor = create_member('novel-editor', 'Redaktor')
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

    def test_multiple_roles_and_existing_verifier_separation(self):
        verifier = create_member('novel-verifier', 'Weryfikator')
        with self.assertRaises(ValidationError):
            self.batch([self.first.pk], [('editor', self.editor), ('verifier_1', verifier), ('verifier_2', verifier)])
        self.assertFalse(self.first.workflow_role_assignments.exists())
        self.batch([self.first.pk], [('editor', self.editor), ('verifier_1', verifier)])
        self.assertEqual(self.first.workflow_role_assignments.count(), 2)

    def test_search_and_intake_keep_books_separate(self):
        self.assertEqual(_search_anthologies('Powieść'), [])
        self.assertEqual(_search_anthologies('Powieść', novels=True)[0]['pk'], self.book.pk)
        self.assertFalse(SingleReviewForm().fields['anthology'].queryset.filter(pk=self.book.pk).exists())
        self.assertContains(self.client.get(reverse('core:global_search'), {'query': 'Powieść'}), 'Powieść testowa')

    def test_chapter_admin_form_accepts_inherited_readonly_authors(self):
        request = RequestFactory().get('/')
        request.user = self.admin
        form_class = admin.site._registry[Text].get_form(request, self.first)
        form = form_class({'title': self.first.title, 'chapter_number': 1, 'length': 100}, instance=self.first)
        self.assertNotIn('authors', form.fields)
        self.assertTrue(form.is_valid(), form.errors)

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
        response = self.post(action='finish')
        self.assertEqual(response.status_code, 400)
        # Production errors expand the otherwise collapsed task panel.
        self.assertTrue(html.fromstring(response.content).xpath('//details[@id="novel-production"][@open]'))
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


class NovelPanelTests(NovelData, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.replacement = Author.objects.create(first_name='Anna', last_name='Nowak', email=None)
        cls.second = new_chapter(cls.book, cls.book.novel, chapter_number=2, length=4000)

    def setUp(self):
        super().setUp()
        self.admin_url = reverse('admin:texts_novelprofile_change', args=[self.book.novel.pk])

    def admin_data(self):
        response = self.client.get(self.admin_url)
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        data = {node.get('name'): node.get('value', '') for node in doc.xpath('//input[@type="hidden"][@name]')}
        data.update(title='Nowy tytuł', authors=[self.replacement.pk], tags='magia, wojna\nmagia',
                    genre='fantasy', content_warnings='przemoc', file_url='https://example.test/book', notes='Ustalenia')
        return data

    def test_admin_edit_synchronizes_authors_and_vocabulary_to_chapters(self):
        response = self.client.post(self.admin_url, self.admin_data())
        self.assertEqual(response.status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, 'Nowy tytuł')
        profile = NovelProfile.objects.get(anthology=self.book)
        self.assertEqual(profile.authors.get(), self.replacement)
        self.assertEqual(profile.file_url, 'https://example.test/book')
        self.assertEqual(profile.content_warnings, 'przemoc')
        for chapter in self.book.texts.all():
            self.assertEqual(chapter.authors.get(), self.replacement)
            self.assertEqual(chapter.tags, 'magia, wojna')
            self.assertEqual(chapter.genre, 'fantasy')
            self.assertEqual(chapter.workflow_stages.count(), 1)

    def test_admin_rejects_stale_data_and_does_not_partially_save(self):
        data = self.admin_data()
        self.chapter.length = 777
        self.chapter.save(update_fields=['length'])
        response = self.client.post(self.admin_url, data)
        self.assertIn(response.status_code, (200, 409))
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, 'Książka z rozdziałami')
        self.assertEqual(self.book.novel.authors.get(), self.author)

    def test_member_cannot_edit_admin_and_no_frontend_metadata_action(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.admin_url).status_code, 302)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'Edytuj dane w panelu admina')
        self.client.force_login(self.admin)
        Text.objects.filter(pk=self.chapter.pk).update(tags='magia')
        response = self.client.post(self.url, {'novel_token': edit_token(self.book, self.admin),
            'action': 'metadata', 'title': 'Nie zmieniaj', 'authors': [self.replacement.pk], 'tags': 'smoki',
            'genre': 'fantasy'})
        self.assertEqual(response.status_code, 400)
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, 'Książka z rozdziałami')
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.tags, 'magia')

    def test_split_layout_single_bulk_action_and_length_values(self):
        response = self.client.get(self.url)
        doc = html.fromstring(response.content)
        overview = doc.xpath('//div[contains(@class,"novel-overview-grid")]')[0]
        self.assertEqual(overview.xpath('./section/@aria-labelledby'), ['chapter-add-heading', 'novel-metadata-heading'])
        self.assertFalse(overview.xpath('.//table'))
        table_card = overview.xpath('following-sibling::section[1]')[0]
        self.assertEqual(table_card.get('aria-labelledby'), 'chapter-table-heading')
        self.assertTrue(table_card.xpath('.//table'))
        self.assertEqual(len(doc.xpath('//form[@id="chapter-assign"]//select[@name="assign-role"]')), 1)
        self.assertEqual(len(doc.xpath('//form[@id="chapter-assign"]//select[@name="assign-assignee"]')), 1)
        self.assertFalse(doc.xpath('//input[@name="assign-TOTAL_FORMS"]|//input[@name="chapter_selection"]'))
        self.assertFalse(doc.xpath('//form//form'))
        self.assertContains(response, '4000')
        self.assertContains(response, 'Brak danych')
        self.assertNotContains(response, 'Końcowa kontrola spójności')
        self.assertNotContains(response, 'name="action" value="metadata"')

    def test_chapter_tables_and_story_work_stages_skip_pagination(self):
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.chapter.pk]))
        tables = html.fromstring(response.content).xpath('//main//table')
        self.assertTrue(tables)
        self.assertTrue(all(table.get('data-pagination') == 'off' for table in tables))
        story = Text.objects.create(title='Opowiadanie', length=100)
        response = self.client.get(reverse('core:assigned_text_detail', args=[story.pk]))
        doc = html.fromstring(response.content)
        self.assertEqual(doc.xpath('//section[@aria-labelledby="workflow-heading"]//table/@data-pagination'), ['off'])
        self.assertEqual(doc.xpath('//*[@id="text-stage-history"]//table/@data-pagination'), ['off'])

    def test_old_approval_fields_removed(self):
        self.assertFalse({'approved_at', 'approved_by', 'approved_signature'} & {field.name for field in NovelProfile._meta.fields})


class NovelProductionTests(NovelData, TestCase):
    def test_new_novel_cannot_skip_completion_gate(self):
        """Błąd: nową powieść dało się od razu zapisać jako gotową z pominięciem kontroli rozdziałów."""
        book = Anthology(title='Pusta gotowa', is_novel=True, status='ready')
        for action in (book.full_clean, book.save):
            with self.assertRaises(ValidationError):
                action()
        self.assertFalse(Anthology.objects.filter(title=book.title).exists())
        # Existing archive anthologies remain supported.
        Anthology.objects.create(title='Archiwum', status='ready')

    def test_novels_have_no_production_controls_and_no_new_illustrations(self):
        """Błąd: rozdziały powieści dostawały ilustracje i akcję audiobooka mimo braku tej produkcji."""
        self.book.has_illustrations = True
        self.book.save()
        second = new_chapter(self.book, self.book.novel, chapter_number=2)
        self.assertEqual(sync_required_illustrations(self.book), 0)
        self.assertFalse(Illustration.objects.filter(text__anthology=self.book).exists())
        with self.assertRaises(ValidationError):
            Illustration.objects.create(text=second)
        # Simulate a record saved before the scope validator existed.
        legacy = Illustration(text=second)
        Illustration.objects.bulk_create([legacy])
        # MySQL does not populate AutoField PKs after bulk_create().
        legacy = Illustration.objects.get(text=second)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.chapter.pk]))
        doc = html.fromstring(response.content)
        self.assertFalse(doc.xpath('//*[@id="text-audiobook" or @id="text-illustration"]'))
        # A valid optimistic-lock token still cannot submit the removed novel action.
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        url = reverse('core:update_text_audiobook', args=[self.chapter.pk])
        self.assertEqual(self.client.post(url, {'for_recording': 'on', '_edit_version': token}).status_code, 404)
        self.assertNotContains(self.client.get(reverse('illustrations:illustration_list')), self.book.title)
        self.assertEqual(self.client.get(reverse('illustrations:illustration_detail', args=[legacy.pk])).status_code, 404)
        self.assertTrue(Illustration.objects.filter(pk=legacy.pk).exists())


    def test_all_chapters_visible_with_badges_buttons_and_collapsed_tasks(self):
        add_chapters(self.book, self.book.novel, list(range(2, 32)))
        WorkflowRoleAssignment.objects.create(text=self.chapter, role='editor', assigned_to=self.editor)
        response = self.client.get(reverse('core:novel_detail', args=[self.book.pk]), {'page_size': '25', 'page': '2'})
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//input[@name="chapters"]')), 31)
        self.assertTrue(doc.xpath('//table[@data-pagination="off"]'))
        badges = doc.xpath('//span[contains(@class,"role-badge")][@data-role="editor"]')
        self.assertTrue(badges)
        self.assertNotIn('Redaktor', badges[0].text_content())
        self.assertEqual(badges[0].get('title'), 'Redaktor')
        self.assertEqual(len(doc.xpath('//a[contains(@class,"secondary-button")][text()="Edytuj"]')), 31)
        self.assertTrue(doc.xpath('//details[@id="novel-production"][not(@open)]'))
        self.assertIsNone(response.context.get('page_obj'))
        self.assertNotContains(response, 'name="has_illustrations"')
        self.assertContains(self.client.get(reverse('core:novel_list')), '500')


class NovelPageEdgeTests(NovelData, TestCase):
    """Lista i strona powieści: wyszukiwanie, filtry i odrzucanie nieprawidłowych formularzy bez zapisu."""

    def post(self, **data):
        return self.client.post(self.url, {'novel_token': edit_token(self.book, self.admin), **data})

    def production_data(self, **changes):
        response = self.client.get(self.url)
        data = {'action': 'production'}
        for form in response.context['task_forms']:
            data[form.prefix + '-status'] = form['status'].value()
            data[form.prefix + '-assigned_to'] = form['assigned_to'].value() or ''
        cover = response.context['cover_form']
        data.update({name: cover[name].value() or '' for name in ('cover_status', 'print_status')})
        data.update(changes)
        return data

    def test_list_searches_title_and_public_author_name_and_filters_status(self):
        plain = Author.objects.create(first_name='Ewa', last_name='Bezpseudonimu', email=None)
        other = Anthology.objects.create(title='Druga powieść', is_novel=True)
        other.novel.authors.add(plain)
        Anthology.objects.create(title='Porzucona powieść', is_novel=True, status='abandoned')
        url = reverse('core:novel_list')

        def titles(**params):
            return [book.title for book in self.client.get(url, params).context['page_obj']]

        self.assertEqual(titles(), ['Druga powieść', 'Książka z rozdziałami'])
        self.assertEqual(titles(q='Pióro'), ['Książka z rozdziałami'])
        self.assertEqual(titles(q='Bezpseudonimu'), ['Druga powieść'])
        self.assertEqual(titles(q='Ewa'), ['Druga powieść'])
        # Prawdziwe nazwisko autora z pseudonimem nie jest kluczem wyszukiwania.
        self.assertEqual(titles(q='Kowalski'), [])
        self.assertEqual(titles(q='druga'), ['Druga powieść'])
        self.assertEqual(titles(status='ready'), [])
        self.assertEqual(titles(status='nieznany'), titles())
        self.assertEqual(titles(sort='-title'), ['Książka z rozdziałami', 'Druga powieść'])
        self.assertEqual(titles(sort='pk; drop'), titles())

    def test_failed_author_save_rolls_back_new_novel(self):
        with patch('core.views.novels.NovelForm.save_authors', side_effect=IntegrityError):
            response = self.client.post(reverse('core:novel_add'), {
                'title': 'Wyścig', 'new_author_first_name': 'Ola', 'new_author_last_name': 'Nowa'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Nie zapisano powieści', status_code=400)
        self.assertFalse(Anthology.objects.filter(title='Wyścig').exists())

    def test_invalid_chapter_and_assignment_forms_save_nothing(self):
        cases = [
            ({'action': 'chapter', 'chapter_number': ''}, 'Popraw dane rozdziału.'),
            ({'action': 'assign', 'chapters': ['abc'], 'assign-role': 'editor',
              'assign-assignee': self.editor.pk}, 'Nieprawidłowy wybór rozdziałów.'),
            ({'action': 'assign', 'chapters': ['１'], 'assign-role': 'editor',
              'assign-assignee': self.editor.pk}, 'Nieprawidłowy wybór rozdziałów.'),
            ({'action': 'assign', 'chapters': [self.chapter.pk], 'assign-role': 'editor'},
             'Wybierz rolę i członka zespołu'),
            ({'action': 'nieznana'}, 'Nieznana operacja.'),
        ]
        for data, message in cases:
            with self.subTest(data=data):
                response = self.post(**data)
                self.assertContains(response, message, status_code=400)
        self.assertEqual(self.book.texts.count(), 1)
        self.assertFalse(self.chapter.workflow_role_assignments.exists())

    def test_production_tasks_are_saved_together_or_not_at_all(self):
        person = self.editor.person_profile
        invalid = self.production_data(**{'blurb-status': 'ready'})
        response = self.post(**invalid)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Popraw zadania produkcyjne.', status_code=400)
        self.assertTrue(html.fromstring(response.content).xpath('//details[@id="novel-production"][@open]'))
        self.assertEqual(self.book.production_tasks.get(task_type='blurb').status, 'not_commissioned')
        valid = self.production_data(**{'blurb-status': 'commissioned', 'blurb-assigned_to': person.pk,
                                        'cover_status': 'in_progress', 'new_cover_first_name': 'Ola',
                                        'new_cover_last_name': 'Okładkowa'})
        self.assertEqual(self.post(**valid).status_code, 302)
        task = self.book.production_tasks.get(task_type='blurb')
        self.assertEqual((task.status, task.assigned_to), ('commissioned', person))
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_status, 'in_progress')
        self.assertEqual(self.book.cover_author, 'Ola Okładkowa')

    def test_chapter_search_accepts_only_chapter_numbers(self):
        new_chapter(self.book, self.book.novel, chapter_number=2)
        for query, expected in ((' 2 ', [2]), ('Rozdział', []), ('9' * 11, [])):
            with self.subTest(query=query):
                rows = self.client.get(self.url, {'q': query}).context['rows']
                self.assertEqual([row['chapter'].chapter_number for row in rows], expected)
