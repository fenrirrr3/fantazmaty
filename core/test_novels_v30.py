from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from texts.models import Anthology, Text, NovelProfile
from texts.novels import edit_token, new_chapter
from workflow.tests import create_member


class NovelPanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v30-admin', 'v30-admin@example.test', 'test')
        cls.member = create_member('v30-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Powieść v30', is_novel=True)
        cls.author = Author.objects.create(first_name='Jan', last_name='Kowalski', email=None)
        cls.replacement = Author.objects.create(first_name='Anna', last_name='Nowak', email=None)
        cls.book.novel.authors.add(cls.author)
        cls.chapter = new_chapter(cls.book, cls.book.novel, chapter_number=1)
        cls.second = new_chapter(cls.book, cls.book.novel, chapter_number=2, length=4000)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('core:novel_detail', args=[self.book.pk])
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
        self.assertEqual(self.book.title, 'Powieść v30')
        self.assertEqual(self.book.novel.authors.get(), self.author)

    def test_member_cannot_edit_admin_and_no_frontend_metadata_action(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.admin_url).status_code, 302)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'Edytuj dane w panelu admina')
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {'novel_token': edit_token(self.book, self.admin),
            'action': 'metadata', 'title': 'Nie zmieniaj', 'authors': [self.replacement.pk]})
        self.assertEqual(response.status_code, 400)
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, 'Powieść v30')

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

    def test_bulk_assigns_only_checked_chapters_and_preserves_work(self):
        response = self.client.post(self.url, {'novel_token': edit_token(self.book, self.admin),
            'action': 'assign', 'chapters': [self.second.pk], 'assign-role': 'editor', 'assign-assignee': self.member.pk})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.chapter.workflow_role_assignments.exists())
        self.assertEqual(self.second.workflow_role_assignments.get().assigned_to, self.member)
        self.assertIsNone(self.second.workflow_stages.get().started_at)

    def test_chapter_length_optional_and_regular_story_still_requires_it(self):
        url = reverse('core:chapter_edit', args=[self.book.pk, self.chapter.pk])
        for length, expected in [('1500', 1500), ('', None)]:
            response = self.client.post(url, {'novel_token': edit_token(self.book, self.admin),
                                            'chapter_number': 1, 'length': length})
            self.assertEqual(response.status_code, 302)
            self.chapter.refresh_from_db()
            self.assertEqual(self.chapter.length, expected)
        response = self.client.post(url, {'novel_token': edit_token(self.book, self.admin), 'chapter_number': 1, 'length': 0})
        self.assertEqual(response.status_code, 400)

    def test_chapter_tables_skip_pagination_but_regular_story_does_not(self):
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.chapter.pk]))
        tables = html.fromstring(response.content).xpath('//main//table')
        self.assertTrue(tables)
        self.assertTrue(all(table.get('data-pagination') == 'off' for table in tables))
        story = Text.objects.create(title='Opowiadanie', length=100)
        response = self.client.get(reverse('core:assigned_text_detail', args=[story.pk]))
        self.assertFalse(html.fromstring(response.content).xpath('//main//table[@data-pagination="off"]'))

    def test_old_approval_fields_removed(self):
        self.assertFalse({'approved_at', 'approved_by', 'approved_signature'} & {field.name for field in NovelProfile._meta.fields})
