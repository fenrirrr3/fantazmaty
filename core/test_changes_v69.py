from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import html

from core.models import Audiobook, AudiobookStage
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.tests import create_member


class ReviewHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v69admin', 'v69@example.test', 'test')
        cls.member = create_member('v69reader', 'Recenzent')
        cls.other = create_member('v69other', 'Recenzent')
        cls.book = Anthology.objects.create(title='Porzucona historia', status='abandoned')
        cls.normal = Anthology.objects.create(title='Bieżąca antologia')
        cls.assignments = {}
        for title, opinion, archived, hidden, user in [
            ('Oddana recenzja', 'yes', False, False, cls.member),
            ('Archiwalna recenzja', 'no', True, False, cls.member),
            ('Niedokończona recenzja', 'reading', False, False, cls.member),
            ('Ukryta recenzja', 'yes', False, True, cls.member),
            ('Cudza recenzja', 'yes', False, False, cls.other),
        ]:
            review = Review.objects.create(title=title, anthology=cls.book, length=1,
                old_reviews=archived, is_hidden=hidden, status="accepted" if archived else "new")
            cls.assignments[title] = ReviewAssignment.objects.create(review=review, user=user,
                position=1, opinion=opinion, notes='Zachowana treść recenzji')

    def test_own_completed_and_archived_remain_without_abandoned_pending_work(self):
        self.client.force_login(self.member)
        expected = {'completed': ['Archiwalna recenzja', 'Oddana recenzja'], 'all': ['Archiwalna recenzja', 'Oddana recenzja'],
            'archived': ['Archiwalna recenzja', 'Oddana recenzja'], 'active': [], 'waiting': []}
        for view, titles in expected.items():
            with self.subTest(view=view):
                page = self.client.get(reverse('core:my_reviews'), {'view': view})
                self.assertEqual(page.status_code, 200)
                self.assertEqual([a.review.title for a in page.context['assignments']], titles)
                self.assertEqual(list(page.context['anthologies'].values_list('pk', flat=True)),
                    [self.book.pk] if titles else [])
        filtered = self.client.get(reverse('core:my_reviews'),
            {'view': 'completed', 'anthology': self.book.pk, 'q': 'Oddana', 'opinion': ['yes'], 'sort': 'title'})
        self.assertEqual(filtered.context['page_obj'].paginator.count, 1)
        self.assertNotContains(self.client.get(reverse('core:review_list')), 'Oddana recenzja')

    def test_completed_detail_readable_but_not_editable_or_accessible_to_unrelated_reader(self):
        item = self.assignments['Oddana recenzja']
        url = reverse('core:assigned_review_detail', args=[item.review_id])
        self.client.force_login(self.member)
        page = self.client.get(url)
        self.assertContains(page, 'Zachowana treść recenzji')
        self.assertTrue(page.context['is_review_locked'])
        self.assertIsNone(page.context['opinion_form'])
        self.assertIn(self.client.post(url, {'opinion': 'no', 'notes': 'Zmiana'}).status_code, (403, 404))
        item.refresh_from_db()
        self.assertEqual(item.opinion, 'yes')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(html.fromstring(page.content).xpath('//form[@method="post"][contains(@action,"recenzje")]'))

    def test_profiles_preserve_both_histories_and_archive_links_work_for_admin(self):
        url = reverse('core:person_detail', args=[self.member.person_profile.pk])
        self.client.force_login(self.member)
        page = self.client.get(url)
        self.assertEqual([a.review.title for a in page.context['completed_reviews']], ['Oddana recenzja'])
        self.assertEqual([a.review.title for a in page.context['archived_reviews']], ['Archiwalna recenzja'])
        self.assertNotContains(page, 'Ukryta recenzja')
        self.client.force_login(self.admin)
        archive = self.assignments['Archiwalna recenzja']
        self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[archive.review_id])).status_code, 200)

    def test_copied_text_abandoned_scope_and_historical_person_are_retained(self):
        text = Text.objects.create(title='Kopia w porzuconej', anthology=self.book, length=1)
        review = Review.objects.create(title='Zmieniona antologia kopii', anthology=self.normal,
            length=1, copied_text=text)
        ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion='yes')
        old = Review.objects.create(title='Historyczne przypisanie', anthology=self.book, length=1, old_reviews=True)
        ReviewAssignment.objects.create(review=old, historical_person=self.member.person_profile,
            position=1, opinion='yes')
        self.client.force_login(self.member)
        page = self.client.get(reverse('core:my_reviews'), {'view': 'completed'})
        self.assertContains(page, review.title)
        page = self.client.get(reverse('core:assigned_review_detail', args=[review.pk]))
        self.assertTrue(page.context['is_review_locked'])
        self.assertContains(self.client.get(reverse('core:my_reviews'), {'view': 'archived'}), old.title)

    def test_link_button_disappears_only_for_linked_review(self):
        self.client.force_login(self.admin)
        text = Text.objects.create(title='Tekst z recenzją', anthology=self.normal, length=1)
        url = reverse('core:assigned_text_detail', args=[text.pk])
        self.assertContains(self.client.get(url), 'Powiąż zgłoszenie z recenzjami')
        review = Review.objects.create(title=text.title, anthology=self.normal, length=1, copied_text=text)
        page = self.client.get(url)
        self.assertNotContains(page, 'Powiąż zgłoszenie z recenzjami')
        self.assertContains(page, reverse('core:assigned_review_detail', args=[review.pk]))


class AudioTableTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('v69audio', 'Korektor audiobooków')
        cls.books = [Anthology.objects.create(title=f'Antologia {n}', status='ready') for n in range(3)]
        cls.texts = [Text.objects.create(title=f'Audio {n}', anthology=cls.books[n], length=1) for n in range(3)]
        cls.audios = [Audiobook.objects.create(text=t, status=status) for t, status in
            zip(cls.texts, ('recording', 'editing', 'published'))]
        cls.url = reverse('core:audiobooks')

    def setUp(self):
        self.client.force_login(self.member)

    def test_multi_filters_combine_or_within_and_across_fields_and_survive_pagination(self):
        params = {'anthology': [self.books[0].pk, self.books[1].pk], 'status': ['recording', 'editing']}
        page = self.client.get(self.url, params)
        self.assertEqual({r['pk'] for r in page.context['texts']}, {t.pk for t in self.texts[:2]})
        doc = html.fromstring(page.content)
        self.assertEqual(len(doc.xpath('//select[@multiple]')), 2)
        self.assertEqual(len(doc.xpath('//select[@multiple]/option[@selected]')), 4)
        for value in ('invalid', '-1', '99999999999999999999999999999'):
            self.assertEqual(self.client.get(self.url, {**params, 'anthology': [self.books[0].pk, value]}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {**params, 'status': ['invalid']}).context['page_obj'].paginator.count, 0)
        for n in range(27):
            text = Text.objects.create(title=f'Kolejne {n}', anthology=self.books[0], length=1)
            Audiobook.objects.create(text=text, status='recording')
        page = self.client.get(self.url, {**params, 'page_size': '25', 'page': 2})
        self.assertEqual(page.context['page_obj'].paginator.count, 29)
        self.assertEqual(len(page.context['texts']), 4)
        self.assertContains(page, 'status=recording&amp;status=editing')

    def test_date_ranges_preserve_repeated_and_incomplete_history_and_legacy_start(self):
        audio = self.audios[0]
        audio.recording_started_at = date(2020, 1, 1)  # History takes precedence over stale scalar date.
        audio.proofreading_started_at = date(2026, 1, 4)
        audio.save()
        for start, end, kind in [(None, date(2026, 1, 2), 'recording'),
                (date(2026, 2, 1), None, 'recording'), (None, None, 'editing')]:
            AudiobookStage.objects.create(text=audio.text, stage_type=kind, started_at=start,
                ended_at=end, is_completed=bool(end))
        page = self.client.get(self.url, {'q': 'Audio 0'})
        row = list(page.context['texts'])[0]
        self.assertEqual(row['periods']['recording'], [
            {'started_at': None, 'ended_at': date(2026, 1, 2)},
            {'started_at': date(2026, 2, 1), 'ended_at': None}])
        self.assertEqual(row['periods']['proofreading'], [{'started_at': date(2026, 1, 4), 'ended_at': None}])
        self.assertEqual(row['periods']['editing'], [{'started_at': None, 'ended_at': None}])
        self.assertNotContains(page, '01.01.2020')
        self.assertNotContains(page, 'Czeka na publikację od')
        self.assertContains(page, '02.01.2026')
        self.assertEqual(len(html.fromstring(page.content).xpath('//table/thead/tr/th')), 14)
        for sort in ('recording', '-recording', 'proofreading', 'corrections', 'editing', 'engineer'):
            self.assertEqual(self.client.get(self.url, {'sort': sort}).status_code, 200)
        AudiobookStage.objects.create(text=self.texts[1], stage_type='recording', started_at=date(2026, 1, 15))
        params = {'anthology': [b.pk for b in self.books[:2]], 'sort': 'recording'}
        self.assertEqual([r['pk'] for r in self.client.get(self.url, params).context['texts']],
            [self.texts[1].pk, self.texts[0].pk])

    def test_history_prefetch_query_count_does_not_grow_with_rows(self):
        self.client.get(self.url)  # Warm session/permission paths.
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url)
        for n in range(6):
            text = Text.objects.create(title=f'Historia {n}', anthology=self.books[0], length=1)
            Audiobook.objects.create(text=text)
            AudiobookStage.objects.create(text=text, stage_type='editing')
        with CaptureQueriesContext(connection) as more:
            self.client.get(self.url)
        self.assertEqual(len(few), len(more))


class CompletionLabelsTests(TestCase):
    def test_profile_finished_labels_cover_media_and_preserve_active_work(self):
        member = create_member('v69work', 'Redaktor')
        book = Anthology.objects.create(title='Ukończone prace', status='ready')
        text = Text.objects.create(title='Gotowy tekst', anthology=book, length=1)
        WorkflowRoleAssignment.objects.create(text=text, assigned_to=member, role='editor')
        WorkflowStage.objects.create(text=text, stage_type='ready', is_current=True)
        audio = Audiobook.objects.create(text=text, proofreader=member, status='published')
        AudiobookStage.objects.create(text=text, stage_type='proofreading', performer=member, is_completed=True)
        self.client.force_login(member)
        page = self.client.get(reverse('core:person_detail', args=[member.person_profile.pk]))
        self.assertEqual(page.status_code, 200)
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//*[@id="person-assignments-table"]//span[contains(@class,"completed")]/text()'), ['Zakończone', 'Zakończone'])
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'published')
        # Profile uses the same rows for ordinary stories, translations and chapters.
        for kind in ('text', 'audio', 'post_layout'):
            rows = [dict(pk=n, kind_key=kind, text={'pk': text.pk, 'title': str(n)},
                detail_url=reverse('core:audiobook_detail', args=[text.pk]), state_label='W toku',
                has_completed_work=finished, has_active_work=not finished,
                latest_stage={}, no_detail_link=True, role='editor', get_role_display='Redaktor')
                for n, finished in enumerate((True, False))]
            content = render_to_string('core/person_detail.html', {'person': member.person_profile,
                'assignments': rows, 'user': member})
            table = html.fromstring(content).get_element_by_id('person-assignments-table')
            self.assertEqual(table.xpath('.//span[contains(@class,"stage-status")]/text()'), ['Zakończone', 'W toku'])
