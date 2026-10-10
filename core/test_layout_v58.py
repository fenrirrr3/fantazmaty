from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from texts.models import Anthology, Extract, VocabularyTerm
from texts.novels import new_chapter
from workflow.services import claim_stage, send_to_first_verification
from workflow.tests import WorkflowTestDataMixin


class ClaimPresentationTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.begin_editing()
        self.pending = self.stage('first_verification')
        self.client.force_login(self.verifier_1)

    def claim_form(self, route):
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200)
        action = reverse('core:take_workflow_stage', args=[self.pending.pk])
        return html.fromstring(response.content).xpath(f'//form[@action="{action}"]')[0]

    def test_reservation_has_no_date_in_list_and_detail_and_stays_unstarted(self):
        for route in [reverse('core:available_texts'), reverse('core:assigned_text_detail', args=[self.text.pk])]:
            form = self.claim_form(route)
            self.assertEqual(form.xpath('normalize-space(.//button)'), 'Przejmij')
            self.assertIn('Do rezerwacji', form.text_content())
            self.assertFalse(form.xpath('.//input[@name="started_at"]'))
        response = self.client.post(reverse('core:take_workflow_stage', args=[self.pending.pk]))
        self.assertEqual(response.status_code, 302)
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.started_at)
        self.assertEqual(self.pending.assignment.assigned_to_id, self.verifier_1.pk)

    def test_handoff_exposes_date_and_post_honors_chosen_date(self):
        send_to_first_verification(self.text, self.editor, self.today)
        for route in [reverse('core:available_texts'), reverse('core:assigned_text_detail', args=[self.text.pk])]:
            form = self.claim_form(route)
            self.assertTrue(form.xpath('.//input[@name="started_at"]'))
            self.assertNotIn('Do rezerwacji', form.text_content())
        date = self.today + timedelta(days=2)
        response = self.client.post(reverse('core:take_workflow_stage', args=[self.pending.pk]), {'started_at': date.isoformat()})
        self.assertEqual(response.status_code, 302)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.started_at, date)

    def test_date_before_handoff_is_rejected_without_assignment(self):
        with self.assertRaises(ValidationError):
            claim_stage(self.text, 'first_verification', self.verifier_1, self.today)
        self.client.post(reverse('core:take_workflow_stage', args=[self.pending.pk]), {'started_at': self.today.isoformat()})
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.started_at)
        self.assertIsNone(self.pending.assignment_id)

    def test_chapter_pairs_notes_and_reviews_without_tag_editor(self):
        self.client.force_login(self.superuser)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = new_chapter(novel, novel.novel, chapter_number=1)
        response = self.client.get(reverse('core:assigned_text_detail', args=[chapter.pk]))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        pair = doc.xpath('//div[contains(@class,"chapter-notes-reviews")]')[0]
        self.assertTrue(pair.xpath('./section[@aria-labelledby="text-notes-heading"]'))
        self.assertTrue(pair.xpath('./details[contains(@class,"review-summary-details")]'))
        self.assertFalse(doc.xpath('//*[@id="text-tags-heading"]'))
        self.assertEqual(len(doc.xpath('//details[contains(@class,"review-summary-details")]')), 1)


class SplitVocabularyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('dictionary58', 'd58@example.test', 'test')
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

    def test_separate_add_forms_save_kind_and_keep_errors_in_correct_column(self):
        response = self.client.get(self.url)
        doc = html.fromstring(response.content)
        self.assertEqual(doc.xpath('//input[@name="kind"]/@value'), ['genre', 'tag'])
        for kind in ('genre', 'tag'):
            self.assertEqual(self.client.post(self.url, {'kind': kind, 'name': 'Nowe hasło'}).status_code, 302)
        self.assertEqual(VocabularyTerm.objects.filter(name='Nowe hasło').count(), 2)
        response = self.client.post(self.url, {'kind': 'genre', 'name': ''})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['sections'][0]['form'].errors)
        self.assertFalse(response.context['sections'][1]['form'].errors)

    def test_extracts_keep_anthology_column_without_group_rows_or_add_button(self):
        author = Author.objects.create(first_name='Anna', last_name='Test')
        Extract.objects.create(author=author, full_name='Anna Test', email='test@example.test',
                               title='Fragment', recruitment='Ekstrakty 1')
        response = self.client.get(reverse('core:extract_list'))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        self.assertFalse(doc.xpath('//tr[@class="extract-group"]'))
        self.assertNotContains(response, 'Dodaj ekstrakt')
        self.assertTrue(doc.xpath('//thead//th[normalize-space()="Antologia"]'))
        self.assertTrue(doc.xpath('//tbody//td[normalize-space()="Ekstrakty 1"]'))
