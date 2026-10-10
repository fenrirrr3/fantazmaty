import io
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.models import Audiobook, AudiobookStage
from core.source_reviews import linkable_reviews, link_source_review
from texts.extract_whole import SOURCE, ensure_whole_text
from texts.models import Anthology, AnthologyTask, Extract, ExtractTextLink, Review, Text
from workflow.models import WorkflowStage
from workflow.tests import create_member


class ChangesV77Tests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('root-v77', '', 'test')
        self.manager = create_member('manager-v77', 'Koordynator redakcji')
        self.book = Anthology.objects.create(title='Testowa antologia')
        self.author = Author.objects.create(first_name='Ala', last_name='Autorka')
        self.text = Text.objects.create(title='Testowe opowiadanie', anthology=self.book, length=100)
        self.text.authors.add(self.author)

    def review(self, **values):
        return Review.objects.create(title=self.text.title, anthology=self.book,
            author=self.author, length=100, **values)

    def test_archived_reviews_searchable_and_linkable_without_changing_decision(self):
        archive = self.review(old_reviews=True, status='new')
        self.assertIn(archive, linkable_reviews(self.text))
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=archive.pk)
        link_source_review(user=self.admin, text_id=self.text.pk, review_id=archive.pk, confirm_mismatch=True)
        archive.refresh_from_db()
        self.assertEqual(archive.copied_text, self.text)
        self.assertEqual(archive.status, 'new')
        self.assertTrue(archive.old_reviews)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:link_text_review', args=[self.text.pk]), {'q': 'Testowe'})
        self.assertContains(response, f'id="source-{archive.pk}"')

    def test_other_anthology_and_rejected_archive_allowed_but_already_linked_protected(self):
        rejected = self.review(status='rejected')
        other = self.review(old_reviews=True, status='accepted')
        other.anthology = Anthology.objects.create(title='Inna')
        other.save()
        linked = self.review(old_reviews=True, status='accepted',
                             copied_text=Text.objects.create(title='Inny', length=1))
        self.assertIn(other, linkable_reviews(self.text))
        # Rejected submissions are never offered and cannot be linked, even with confirmation.
        self.assertNotIn(rejected, linkable_reviews(self.text))
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=rejected.pk, confirm_mismatch=True)
        for review in (linked,):
            self.assertNotIn(review, linkable_reviews(self.text))
            with self.assertRaises(ValidationError):
                link_source_review(user=self.admin, text_id=self.text.pk, review_id=review.pk, confirm_mismatch=True)

    def test_unlinked_list_permissions_and_exact_scope(self):
        ready = Anthology.objects.create(title='Gotowe', status='ready')
        Text.objects.create(title='Już wydany', anthology=ready, length=100)
        Text.objects.create(title='Bez antologii', length=100)
        linked = Text.objects.create(title='Powiązany', anthology=self.book, length=100)
        self.review(status='accepted', copied_text=linked)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('core:unlinked_reviews')).status_code, 403)
        self.assertNotContains(self.client.get(reverse('core:review_list')), 'Niepowiązane')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('core:review_list')), 'Niepowiązane')
        page = self.client.get(reverse('core:unlinked_reviews'))
        self.assertEqual([t['pk'] for t in page.context['texts']], [self.text.pk])
        self.assertContains(page, 'Powiąż recenzje')
        page = self.client.get(reverse('core:unlinked_reviews'), {'q': 'Autorka'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)

    def test_tasks_one_row_six_status_columns_and_cover_dates(self):
        self.book.cover_author = 'Ilustrator'
        self.book.cover_status = 'in_progress'
        with patch('texts.models.timezone.localdate', return_value=date(2026, 9, 1)):
            self.book.save(update_fields=['cover_status', 'cover_author'])
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_commissioned_at, date(2026, 9, 1))
        self.book.cover_status = 'ready'
        self.book.save(update_fields=['cover_status'])
        self.assertEqual(self.book.cover_commissioned_at, date(2026, 9, 1))
        self.client.force_login(self.manager)
        page = self.client.get(reverse('core:task_list'))
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        tree = html.fromstring(page.content)
        self.assertEqual(len(tree.xpath('//table/thead/tr/th')), 7)
        self.assertContains(page, '01.09.2026')
        self.assertContains(page, 'Typografia okładki')
        for key in ['cover', *AnthologyTask.TaskType.values]:
            self.assertEqual(self.client.get(reverse('core:task_list'), {'sort': key}).status_code, 200)
        self.book.cover_status = 'not_started'
        self.book.cover_author = ''
        self.book.save(update_fields=['cover_status', 'cover_author'])
        self.assertIsNone(self.book.cover_commissioned_at)

    def test_unassigned_audio_history_shows_only_waiting_badge(self):
        audio = Audiobook.objects.create(text=self.text, status='proofreading')
        stage = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', started_at=date(2026, 8, 1))
        audio.active_stage = stage
        audio.save()
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:audio_proofreading'), {'hide_completed': '0'})
        self.assertContains(page, 'Oczekuje')
        self.assertNotContains(page, 'Nie zapisano wykonawcy')
        tree = html.fromstring(page.content)
        history = tree.xpath('//ul[contains(@class,"audio-correction-history")]')[0]
        self.assertEqual(' '.join(history.text_content().split()), 'Oczekuje')


class WholeExtractTests(TestCase):
    def test_future_volumes_are_single_workflow_ready_for_assignment(self):
        book = Anthology.objects.create(title='Ekstrakty 4', is_extracts=True)
        text = book.texts.get()
        self.assertEqual(text.title, book.title)
        self.assertEqual(text.workflow_stages.get().stage_type, 'ready_for_editing')
        self.assertFalse(text.authors.exists())
        self.assertFalse(text.workflow_role_assignments.exists())
        self.assertIsNone(text.length)
        text.full_clean()
        book.save()
        self.assertEqual(book.texts.count(), 1)
        with self.assertRaises(ValidationError):
            Text.objects.create(title='Miniatura', length=1, anthology=book)
        from django.forms.models import model_to_dict
        from texts.admin import TextAdminForm
        data = model_to_dict(text)
        form = TextAdminForm(data=data, instance=text)
        self.assertTrue(form.is_valid(), form.errors)
        editor = create_member('extract-editor', 'Redaktor')
        self.client.force_login(editor)
        response = self.client.get(reverse('core:available_texts'))
        self.assertContains(response, book.title)
        for name in ('core:text_list', 'core:workflow_list'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, book.title)
        with self.assertRaises(ValidationError):
            text.anthology = Anthology.objects.create(title='Inna książka')
            text.save(update_fields=['anthology'])
        text.refresh_from_db()
        book.title = 'Ekstrakty 4 – dopisek'
        book.save(update_fields=['title'])
        text.refresh_from_db()
        self.assertEqual(text.title, book.title)

    def test_first_two_ready_third_available_without_fake_dates(self):
        for n in (1, 2, 3):
            book = Anthology.objects.create(title=f'Ekstrakty {n}', is_extracts=True)
            book.refresh_from_db()
            stage = book.texts.get().workflow_stages.get()
            self.assertEqual(book.status, 'ready' if n < 3 else 'in_preparation')
            self.assertEqual(stage.stage_type, 'ready' if n < 3 else 'ready_for_editing')
            self.assertIsNone(stage.started_at)
            self.assertIsNone(stage.ended_at)

    def legacy(self, n, count=2):
        author = Author.objects.create(first_name='Autor', last_name=str(n))
        book = Anthology.objects.create(title=f'Dawny tom {n}')
        source = Extract.objects.create(author=author, full_name=str(author), recruitment=f'Ekstrakty {n}',
            title='Pierwszy\nDrugi', accepted_titles='Pierwszy\nDrugi')
        for name in ['Pierwszy', 'Drugi'][:count]:
            text = Text.objects.create(title=name, anthology=book, import_source='extracts-v1')
            text.authors.add(author)
            WorkflowStage.objects.create(text=text, stage_type='ready' if n < 3 else 'ready_for_editing', is_current=True, is_released=True)
            ExtractTextLink.objects.create(text=text, extract=source, title_key=name.casefold(), source_title=name)
        Anthology.objects.filter(pk=book.pk).update(title=f'Ekstrakty {n}', is_extracts=True)
        book.refresh_from_db()
        return book, source

    def run_conversion(self, apply=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'report.json'
            call_command('consolidate_extract_volumes', apply=apply, report=str(path), stdout=io.StringIO())
            return json.loads(path.read_text(encoding='utf8'))

    def test_conversion_preview_apply_and_repeat_preserve_information(self):
        book, source = self.legacy(1)
        original_source = Extract.objects.values().get(pk=source.pk)
        original_authors = list(Author.objects.values())
        self.run_conversion()
        self.assertEqual(book.texts.count(), 2)
        report = self.run_conversion(True)
        self.assertEqual(report['volumes'][0]['removed_generated_miniatures'], 2)
        self.assertEqual(len(report['volumes'][0]['snapshots']), 2)
        text = book.texts.get()
        self.assertEqual(text.import_source, SOURCE)
        self.assertEqual(text.workflow_stages.get().stage_type, 'ready')
        self.assertEqual(Extract.objects.values().get(pk=source.pk), original_source)
        self.assertEqual(list(Author.objects.values()), original_authors)
        self.assertFalse(ExtractTextLink.objects.exists())
        self.run_conversion(True)
        self.assertEqual(book.texts.get().pk, text.pk)
        self.assertEqual(ensure_whole_text(book).pk, text.pk)

    def test_conversion_rolls_back_all_volumes_on_actual_work(self):
        book, _ = self.legacy(1)
        next_book, _ = self.legacy(3)
        stage = next_book.texts.first().workflow_stages.get()
        stage.started_at = date(2026, 8, 1)
        stage.save()
        with self.assertRaises(CommandError):
            self.run_conversion(True)
        self.assertEqual(book.texts.count(), 2)
        self.assertEqual(next_book.texts.count(), 2)
        self.assertEqual(ExtractTextLink.objects.count(), 4)
