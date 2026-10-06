import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db.models.deletion import ProtectedError
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from illustrations.editing import AssignmentForm, edit_token
from illustrations.models import Illustration, Illustrator
from core.supervision import anthology_credits
from texts.models import Anthology, Text


class MultipleIllustratorsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('illustration32', 'test32@example.test', 'test')
        cls.book = Anthology.objects.create(title='Archiwalna antologia', status='ready', has_illustrations=True)
        cls.story = Text.objects.create(title='Opowiadanie', anthology=cls.book, length=100)
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor')
        cls.story.authors.add(cls.author)
        cls.first = Illustrator.objects.create(first_name='Anna', last_name='Rysująca')
        cls.second = Illustrator.objects.create(first_name='Jan', last_name='Ilustrujący')
        cls.illustration = Illustration.objects.create(text=cls.story)

    def setUp(self):
        self.client.force_login(self.admin)
        self.detail = reverse('illustrations:illustration_detail', args=[self.illustration.pk])
        self.list_url = reverse('illustrations:illustration_list')

    def test_multiple_assignment_and_preserved_selected_inactive_contact(self):
        self.first.is_active = False
        self.first.save()
        self.illustration.set_artists([self.first], status='assigned')
        form = AssignmentForm({'illustrators': [self.first.pk, self.second.pk], 'status': 'delivered', 'version': 'test'}, instance=self.illustration, can_assign=True)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.illustration.illustrators.count(), 2)
        response = self.client.get(self.detail)
        self.assertContains(response, str(self.first))
        self.assertContains(response, str(self.second))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//input[@name="illustrators"][@checked]')), 2)
        self.assertEqual({x['name'] for x in anthology_credits(self.book) if x['role'] == 'Ilustrator'}, {str(self.first), str(self.second)})

    def test_detail_pairs_keep_four_separate_edit_forms(self):
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        grid = doc.xpath('//div[@class="illustration-detail-grid"]')[0]
        self.assertEqual(grid.xpath('./section/@aria-labelledby'), [
            'illustration-assignment-heading', 'illustration-link-heading',
            'illustration-excerpt-heading', 'illustration-notes-heading'])
        self.assertEqual(grid.xpath('./section//form/input[@name="action"]/@value'),
                         ['assignment', 'link', 'excerpt', 'coordinator_notes'])
        self.assertFalse(doc.xpath('//form//form'))
        for section in grid.xpath('./section'):
            self.assertEqual(len(section.xpath('.//input[@name="csrfmiddlewaretoken"]')), 1)
            self.assertTrue(section.xpath('.//input[@name="version"]'))

    def test_published_filter_and_sort_do_not_duplicate_texts(self):
        self.illustration.set_artists([self.first, self.second], status='delivered', preserve_assignment_date=True)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        response = self.client.get(self.list_url, {'hide_published': '0', 'sort': 'illustrator', 'page_size': '500'})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertContains(response, str(self.first))
        self.assertContains(response, str(self.second))
        self.assertContains(self.client.get(self.detail), 'hide_published=0')

    def test_m2m_change_invalidates_stale_post_and_protects_credits(self):
        token = edit_token(self.admin, self.illustration)
        self.illustration.set_artists([self.first], status='delivered')
        response = self.client.post(self.detail, {'action': 'assignment', 'version': token, 'status': 'delivered', 'illustrators': [self.second.pk]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(self.illustration.illustrators.all()), [self.first])
        with self.assertRaises(ProtectedError), transaction.atomic():
            Illustrator.objects.filter(pk=self.first.pk).delete()
        self.assertEqual(self.client.get(reverse('admin:illustrations_illustrator_delete', args=[self.first.pk])).status_code, 403)

    def test_admin_supports_multiple_artists_and_validates_empty_status(self):
        url = reverse('admin:illustrations_illustration_change', args=[self.illustration.pk])
        def token():
            return html.fromstring(self.client.get(url).content).xpath('//input[@name="_edit_version"]/@value')[0]
        payload = {'text': self.story.pk, 'status': 'delivered', 'illustrators': [self.first.pk, self.second.pk], '_save': 'Zapisz', '_edit_version': token()}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.illustration.illustrators.count(), 2)
        payload.update(illustrators=[], _edit_version=token())
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.illustration.illustrators.count(), 2)

    def test_story_management_has_no_author_or_repeat_controls(self):
        self.book.status = 'in_preparation'
        self.book.save()
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.story.pk]))
        self.assertNotContains(response, 'Pokaż kolejkę powtórzeń')
        self.assertNotContains(response, 'text-authors-management-form')
        self.assertContains(response, 'Wycofaj tekst z procesu')
        doc = html.fromstring(response.content)
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        workflow_token = doc.xpath('//input[@name="workflow_token"]/@value')[0]
        response = self.client.post(reverse('core:set_text_authors', args=[self.story.pk]), {'authors': [], '_edit_version': token, 'workflow_token': workflow_token})
        self.assertIn(response.status_code, (403, 409))
        self.assertEqual(list(self.story.authors.all()), [self.author])


class IllustrationImportTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_path = Path(self.tmp.name) / 'import.json'
        self.report_path = Path(self.tmp.name) / 'report.json'
        self.book = Anthology.objects.create(title='Testowa książka', status='ready')
        self.story = Text.objects.create(title='Jeden tekst', anthology=self.book, length=20)
        self.author = Author.objects.create(first_name='Maria', last_name='Autor')
        self.story.authors.add(self.author)
        self.row = {'anthology': self.book.title, 'title': self.story.title, 'author': 'Maria Autor', 'illustrators': ['Anna Artysta', 'Graphos']}

    def run_import(self, *, apply=False, rows=None, aliases=None):
        self.data_path.write_text(json.dumps({'schema_version': 1, 'texts': rows if rows is not None else [self.row], 'illustrator_aliases': aliases or {}}, ensure_ascii=False), encoding='utf-8')
        call_command('import_illustration_credits', str(self.data_path), apply=apply, report=str(self.report_path), stdout=StringIO())
        return json.loads(self.report_path.read_text(encoding='utf-8'))

    def test_preview_rolls_back_apply_is_idempotent_and_dates_unknown(self):
        preview = self.run_import()
        self.assertFalse(Illustration.objects.filter(text=self.story).exists())
        self.assertFalse(Illustrator.objects.exists())
        self.book.refresh_from_db()
        self.assertFalse(self.book.has_illustrations)
        self.assertEqual(preview['counts']['created'], 1)
        users = get_user_model().objects.count()
        saved = self.run_import(apply=True)
        row = Illustration.objects.get(text=self.story)
        self.assertEqual(row.illustrators.count(), 2)
        self.assertEqual(row.status, 'delivered')
        self.assertIsNone(row.assigned_at)
        self.assertFalse(Illustrator.objects.filter(is_active=True).exists())
        self.assertEqual(get_user_model().objects.count(), users)
        self.book.refresh_from_db()
        self.assertTrue(self.book.has_illustrations)
        again = self.run_import(apply=True)
        self.assertEqual(again['counts']['unchanged'], 1)
        self.assertEqual(Illustrator.objects.count(), 2)
        self.assertEqual(saved['mode'], 'zapisano')

    def test_conflict_late_in_batch_rolls_back_every_change_and_writes_report(self):
        with self.assertRaises(CommandError):
            self.run_import(apply=True, rows=[self.row, {**self.row, 'title': 'Brak tekstu'}])
        self.assertFalse(Illustrator.objects.exists())
        self.assertFalse(Illustration.objects.exists())
        self.book.refresh_from_db()
        self.assertFalse(self.book.has_illustrations)
        report = json.loads(self.report_path.read_text(encoding='utf-8'))
        self.assertTrue(report['conflicts'])
        self.assertIn('nic nie zapisano', report['mode'])

    def test_existing_notes_preserved_and_single_artist_extended(self):
        artist = Illustrator.objects.create(first_name='Anna', last_name='Artysta', is_active=True)
        illustration = Illustration(text=self.story, coordinator_notes='Zachowaj tę notatkę', story_url='https://example.org/story')
        illustration.set_artists([artist], status='delivered', preserve_assignment_date=True)
        self.run_import(apply=True)
        illustration.refresh_from_db()
        self.assertEqual(illustration.coordinator_notes, 'Zachowaj tę notatkę')
        self.assertEqual(illustration.story_url, 'https://example.org/story')
        self.assertIsNone(illustration.assigned_at)
        self.assertEqual(illustration.illustrators.count(), 2)
        artist.refresh_from_db()
        self.assertTrue(artist.is_active)

    def test_active_work_and_manual_credit_are_never_overwritten(self):
        illustration = Illustration.objects.create(text=self.story, manual_illustrator_name='Ręczny wpis', status='assigned')
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        illustration.refresh_from_db()
        self.assertEqual(illustration.manual_illustrator_name, 'Ręczny wpis')
        self.assertFalse(Illustrator.objects.exists())

    def test_explicit_alias_reuses_profile_without_changing_its_name(self):
        contact = Illustrator.objects.create(first_name='Anna', last_name='Artysta')
        row = {**self.row, 'illustrators': ['Pseudonim']}
        self.run_import(apply=True, rows=[row], aliases={'Pseudonim': 'Anna Artysta'})
        self.assertEqual(Illustrator.objects.count(), 1)
        self.assertEqual(list(Illustration.objects.get(text=self.story).illustrators.all()), [contact])

    def test_missing_credit_is_skipped_without_creating_anything(self):
        report = self.run_import(apply=True, rows=[{**self.row, 'illustrators': [], 'skip_reason': 'Nieznany ilustrator'}])
        self.assertEqual(report['counts']['skipped'], 1)
        self.assertFalse(Illustrator.objects.exists())
        self.assertFalse(Illustration.objects.exists())
