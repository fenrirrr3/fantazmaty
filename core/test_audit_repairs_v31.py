from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from illustrations.models import Illustration
from illustrations.services import sync_required_illustrations
from texts.models import Anthology, Text, ForeignAuthor
from texts.novels import new_chapter, add_chapters, edit_token
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.tests import create_member


class AuditRepairsV31Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v31-admin', 'v31@example.test', 'test')
        cls.editor = create_member('v31-editor', 'Redaktor')
        cls.reviewer = create_member('v31-reviewer', 'Recenzent')
        cls.book = Anthology.objects.create(title='Powieść v31', is_novel=True)
        cls.author = Author.objects.create(first_name='Jan', last_name='Próbny', email=None)
        cls.book.novel.authors.add(cls.author)
        cls.chapter = new_chapter(cls.book, cls.book.novel, chapter_number=1)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_translation_roundtrip_restores_explicit_author_links_and_keeps_translation(self):
        book = Anthology.objects.create(title='Zwykła antologia')
        text = Text.objects.create(title='Opowiadanie', anthology=book, length=100)
        text.authors.add(self.author)
        book.is_translated = True
        book.save()
        self.assertFalse(text.authors.exists())
        record_id = text.translation.pk
        book.is_translated = False
        book.full_clean()
        book.save(update_fields=['is_translated'])
        self.assertEqual(list(text.authors.all()), [self.author])
        self.assertEqual(text.translation.pk, record_id)
        self.assertEqual(text.translation.foreign_authors.count(), 1)
        # An ordinary edit must not re-add an author deliberately removed afterwards.
        text.authors.clear()
        book.title = 'Nowy tytuł'
        book.save()
        self.assertFalse(text.authors.exists())

    def test_foreign_author_without_mapping_blocks_reverse_conversion_without_partial_save(self):
        book = Anthology.objects.create(title='Tłumaczona antologia', is_translated=True)
        text = Text.objects.create(title='Tłumaczenie', anthology=book, length=100)
        profile = ForeignAuthor.objects.create(first_name='Foreign', last_name='Writer')
        text.translation.foreign_authors.add(profile)
        book.is_translated = False
        for action in (book.full_clean, book.save):
            with self.assertRaises(ValidationError):
                action()
        book.refresh_from_db()
        self.assertTrue(book.is_translated)
        self.assertEqual(text.translation.foreign_authors.get(), profile)

    def test_novel_translation_rejected_before_authors_change(self):
        self.book.is_translated = True
        for action in (self.book.full_clean, self.book.save):
            with self.assertRaises(ValidationError):
                action()
        self.book.refresh_from_db()
        self.assertFalse(self.book.is_translated)
        self.assertEqual(self.chapter.authors.get(), self.author)
        with self.assertRaises(ValidationError):
            Anthology.objects.create(title='Niedozwolona', is_novel=True, is_translated=True)

    def test_new_novel_cannot_skip_completion_gate(self):
        book = Anthology(title='Pusta gotowa', is_novel=True, status='ready')
        for action in (book.full_clean, book.save):
            with self.assertRaises(ValidationError):
                action()
        self.assertFalse(Anthology.objects.filter(title=book.title).exists())
        # Existing archive anthologies remain supported.
        Anthology.objects.create(title='Archiwum', status='ready')

    def test_novels_have_no_production_controls_and_no_new_illustrations(self):
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

    def test_withdrawal_hides_queues_and_reopening_restores_same_records(self):
        book = Anthology.objects.create(title='Antologia ilustracji', has_illustrations=True)
        story = Text.objects.create(title='Znikający tekst', anthology=book, length=100)
        illustration = Illustration.objects.get(text=story)
        illustration.manual_illustrator_name = 'Osoba próbna'
        illustration.status = Illustration.Status.ASSIGNED
        # Use a persisted, model-independent history field for the preservation check.
        illustration.coordinator_notes = 'Nie usuwać historii'
        illustration.save()
        assignment = WorkflowRoleAssignment.objects.create(text=story, role='editor', assigned_to=self.editor)
        withdrawn = WorkflowStage.objects.create(text=story, stage_type='withdrawn', is_released=True)
        def queues():
            return [self.client.get(reverse(name)) for name in ('illustrations:illustration_list', 'core:audiobooks')]
        for response in queues():
            self.assertNotContains(response, story.title)
        illustration.refresh_from_db()
        story.refresh_from_db()
        self.assertEqual(illustration.coordinator_notes, 'Nie usuwać historii')
        self.assertEqual(illustration.manual_illustrator_name, 'Osoba próbna')
        self.assertTrue(story.for_recording)
        self.assertTrue(WorkflowRoleAssignment.objects.filter(pk=assignment.pk).exists())
        # Historic or unreleased withdrawal must not hide an active text.
        withdrawn.is_current = False
        withdrawn.save(update_fields=['is_current'])
        for response in queues():
            self.assertContains(response, story.title)
        self.assertEqual(Illustration.objects.get(text=story).pk, illustration.pk)
        withdrawn.is_current = True
        withdrawn.is_released = False
        withdrawn.save(update_fields=['is_current', 'is_released'])
        for response in queues():
            self.assertContains(response, story.title)

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

    def test_bulk_role_options_and_server_validation_match(self):
        from core.novel_forms import AssignmentForm
        form = AssignmentForm(prefix='assign')
        doc = html.fromstring(str(form))
        option = doc.xpath(f'//select[@name="assign-assignee"]/option[@value="{self.editor.pk}"]')[0]
        self.assertIn('editor', option.get('data-assignment-roles').split())
        self.assertEqual(doc.xpath('//select[@name="assign-assignee"]/@data-assignment-role-select'), ['id_assign-role'])
        invalid = AssignmentForm({'role': 'editor', 'assignee': self.reviewer.pk})
        self.assertFalse(invalid.is_valid())
        valid = AssignmentForm({'role': 'editor', 'assignee': self.editor.pk})
        self.assertTrue(valid.is_valid(), valid.errors)

    def test_production_errors_expand_tasks(self):
        response = self.client.post(reverse('core:novel_detail', args=[self.book.pk]), {
            'novel_token': edit_token(self.book, self.admin), 'action': 'finish',
        })
        self.assertEqual(response.status_code, 400)
        self.assertTrue(html.fromstring(response.content).xpath('//details[@id="novel-production"][@open]'))
