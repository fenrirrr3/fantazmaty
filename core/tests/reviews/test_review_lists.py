"""Listy recenzji: bieżące i archiwum według decyzji, „Moje recenzje”, historia w profilu, sortowanie i filtry
(stan przydziałów, liczba opinii, wyszukiwanie, antologia, kolejka powiadomień autorów)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from lxml import html

from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Review, ReviewAssignment, Reviewers, Text

from .support import ReviewFixtures


class ReviewListByDecisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.reader = create_member('reader', 'Recenzent')
        cls.other = create_member('other', 'Recenzent')
        cls.book = Anthology.objects.create(title='Testowy tom')

    def setUp(self):
        self.client.force_login(self.admin)

    def make_review(self, title, status, opinion='reading', legacy=False, user=None):
        review = Review.objects.create(title=title, anthology=self.book, length=100, status=status,
                                       old_reviews=legacy)
        ReviewAssignment.objects.create(review=review, user=user or self.reader, position=1, opinion=opinion)
        return review

    def test_review_lists_split_by_decision_not_legacy_import_flag(self):
        current = self.make_review('Otwarty', 'new')
        withdrawn = self.make_review('Wycofany', 'withdrawn')
        accepted = self.make_review('Przyjęty', 'accepted', 'yes')
        rejected = self.make_review('Odrzucony', 'rejected', 'no')
        historic = self.make_review('Dawny przyjęty', 'accepted', 'yes', legacy=True)
        url = reverse('core:review_list')
        now = self.client.get(url)
        archive = self.client.get(url, {'old_reviews': '1'})
        # Wycofane zgłoszenia są dostępne tylko w panelu admina.
        self.assertEqual({x['pk'] for x in now.context['reviews']}, {current.pk})
        self.assertNotIn(withdrawn.pk, {x['pk'] for x in archive.context['reviews']})
        self.assertEqual({x['pk'] for x in archive.context['reviews']}, {accepted.pk, rejected.pk, historic.pk})
        self.assertFalse(Review.objects.get(pk=accepted.pk).old_reviews)
        search = self.client.get(reverse('core:global_search'), {'q': 'Przyjęty'})
        self.assertEqual({x['pk'] for x in search.context['reviews']}, {accepted.pk, historic.pk})
        self.assertEqual(self.client.get(reverse('core:global_search'), {'q': 'Wycofany'}).context['reviews'], [])
        self.assertContains(self.client.get(reverse('core:assigned_review_detail', args=[accepted.pk])),
                            'Recenzja archiwalna')

    def test_my_reviews_two_tabs_include_all_history_and_unrated_work(self):
        waiting = self.make_review('Oczekujący', 'new')
        reading = self.make_review('Czytany', 'in_review', 'reading')
        rated = self.make_review('Oceniony przed decyzją', 'new', 'yes')
        rated_in_review = self.make_review('Oceniony w trakcie', 'in_review', 'no')
        accepted = self.make_review('Przyjęty', 'accepted', 'yes')
        historic = self.make_review('Importowany', 'rejected', 'no', legacy=True)
        other = self.make_review('Cudzy', 'new', user=self.other)
        self.client.force_login(self.reader)
        url = reverse('core:my_reviews')
        active = self.client.get(url)
        all_page = self.client.get(url, {'view': 'completed'})
        self.assertEqual({x.review_id for x in active.context['assignments']}, {waiting.pk, reading.pk})
        self.assertEqual(self.client.get(url, {'view': 'active'}).context['page_obj'].paginator.count, 2)
        self.assertNotIn(other.pk, {x.review_id for x in all_page.context['assignments']})
        doc = html.fromstring(all_page.content)
        self.assertEqual(doc.xpath('//nav[@aria-label="Widok moich recenzji"]/a/text()'), ['W toku', 'Oddane'])
        self.assertEqual({x.review_id for x in all_page.context['assignments']},
                         {rated.pk, rated_in_review.pk, accepted.pk, historic.pk})
        self.assertEqual(all_page.context['page_obj'].paginator.count, 4)
        # Dawne zakładki (archived, decided, all) nie są już obsługiwane – pokazują widok domyślny.
        self.assertEqual(self.client.get(url, {'view': 'archived'}).context['selected_view'], 'active')

    def test_profile_archive_uses_decision_and_preserves_hidden_permissions(self):
        accepted = self.make_review('Przyjęty', 'accepted', 'yes')
        current = self.make_review('Wciąż oceniany', 'in_review', 'yes')
        hidden = self.make_review('Ukryty przyjęty', 'accepted', 'yes')
        hidden.is_hidden = True
        hidden.save()
        self.client.force_login(self.reader)
        page = self.client.get(reverse('core:person_detail', args=[self.reader.person_profile.pk]))
        self.assertEqual([a.review_id for a in page.context['archived_reviews']], [accepted.pk])
        self.assertEqual([a.review_id for a in page.context['completed_reviews']], [current.pk])


class ReviewHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
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
            review = Review.objects.create(title=title, anthology=cls.book, length=1, old_reviews=archived,
                                           is_hidden=hidden, status='accepted' if archived else 'new')
            cls.assignments[title] = ReviewAssignment.objects.create(
                review=review, user=user, position=1, opinion=opinion, notes='Zachowana treść recenzji')

    def test_own_completed_and_archived_remain_without_abandoned_pending_work(self):
        self.client.force_login(self.member)
        done = ['Archiwalna recenzja', 'Oddana recenzja']
        expected = {'completed': done, 'active': [], 'waiting': []}
        for view, titles in expected.items():
            with self.subTest(view=view):
                page = self.client.get(reverse('core:my_reviews'), {'view': view})
                self.assertEqual(page.status_code, 200)
                self.assertEqual([a.review.title for a in page.context['assignments']], titles)
                self.assertEqual(list(page.context['anthologies'].values_list('pk', flat=True)),
                                 [self.book.pk] if titles else [])
        filtered = self.client.get(reverse('core:my_reviews'), {
            'view': 'completed', 'anthology': self.book.pk, 'q': 'Oddana', 'opinion': ['yes'], 'sort': 'title'})
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
        self.assertEqual(
            self.client.get(reverse('core:assigned_review_detail', args=[archive.review_id])).status_code, 200)

    def test_copied_text_abandoned_scope_and_historical_person_are_retained(self):
        text = Text.objects.create(title='Kopia w porzuconej', anthology=self.book, length=1)
        review = Review.objects.create(title='Zmieniona antologia kopii', anthology=self.normal, length=1,
                                       copied_text=text)
        ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion='yes')
        old = Review.objects.create(title='Historyczne przypisanie', anthology=self.book, length=1,
                                    old_reviews=True)
        ReviewAssignment.objects.create(review=old, historical_person=self.member.person_profile, position=1,
                                        opinion='yes')
        self.client.force_login(self.member)
        page = self.client.get(reverse('core:my_reviews'), {'view': 'completed'})
        self.assertContains(page, review.title)
        page = self.client.get(reverse('core:assigned_review_detail', args=[review.pk]))
        self.assertTrue(page.context['is_review_locked'])
        self.assertContains(self.client.get(reverse('core:my_reviews'), {'view': 'completed'}), old.title)


class ReviewListSortAndFilterTests(ReviewFixtures):
    def test_sorting_by_author_renders_rows(self):
        review = self.make_review(title='Sortowane', status=Review.Status.IN_REVIEW, author_first_name='Ala',
                                  author_last_name='Kot', email='k@example.test')
        self.assign(self.admin, opinion=Reviewers.Opinion.READING, review=review)
        self.client.force_login(self.admin)
        for sort in ('author', '-author'):
            response = self.client.get(reverse('core:review_list'), {'sort': sort})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Sortowane')

    def test_status_filter_offers_only_supported_statuses_and_no_bulk_actions(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:review_list'))
        values = [value for value, _label in response.context['status_choices']]
        self.assertNotIn(Review.Status.WITHDRAWN, values)
        self.assertIn(Review.Status.NEW, values)
        self.assertNotContains(response, 'Operacje zbiorcze')
        self.assertNotContains(response, 'review-bulk-form')
        doc = html.fromstring(response.content)
        self.assertTrue(doc.xpath('//*[contains(@class,"review-filter-bottom")]//*[@data-active-filters-target]'))

    def test_work_filters_search_and_anthology(self):
        other_book = Anthology.objects.create(title='Drugi tom')
        empty = self.review
        one = self.make_review(title='Jeden')
        self.assign(self.reviewer, review=one)
        done = self.make_review(title='Ukończone')
        self.assign(self.reviewer, review=done, opinion='yes')
        self.assign(self.coordinator, review=done, opinion='no', position=2)
        full = self.make_review(title='Pełne')
        self.assign(self.reviewer, review=full, opinion='yes')
        for position in range(2, ReviewAssignment.MAX_REVIEWERS + 1):
            # Miejsce po usuniętym koncie nadal jest zajęte.
            ReviewAssignment.objects.create(review=full, user=None, position=position)
        elsewhere = self.make_review(title='Gdzie indziej', anthology=other_book, author_last_name='Kowalska')
        everything = {empty.pk, one.pk, done.pk, full.pk, elsewhere.pk}
        cases = [
            (self.admin, {'completed': '0'}, {empty.pk, one.pk, elsewhere.pk}),
            (self.admin, {'completed': '2'}, {done.pk}),
            (self.admin, {'completed': '1'}, {full.pk}),
            (self.admin, {'completed': '9'}, everything),
            (self.admin, {'q': 'pełne'}, {full.pk}),
            (self.admin, {'q': 'drugi tom'}, {elsewhere.pk}),
            (self.admin, {'q': 'Kowalska'}, {elsewhere.pk}),
            # Bez dostępu do danych autorów nazwisko nie jest kryterium wyszukiwania.
            (self.coordinator, {'q': 'Kowalska'}, set()),
            (self.admin, {'anthology': str(other_book.pk)}, {elsewhere.pk}),
            (self.admin, {'anthology': '0'}, everything),
            (self.admin, {'anthology': '١'}, everything),
            (self.admin, {'anthology': '9' * 20}, everything),
        ]
        url = reverse('core:review_list')
        for user, params, expected in cases:
            with self.subTest(user=user.username, **params):
                self.client.force_login(user)
                page = self.client.get(url, params)
                self.assertEqual({row['pk'] for row in page.context['reviews']}, expected)
        page = self.client.get(url, {'anthology': 'abc', 'completed': '-1'})
        self.assertEqual((page.context['selected_anthology_id'], page.context['selected_completed']), (None, ''))

    def test_status_sort_and_author_notification_queues(self):
        today = timezone.localdate()
        Review.objects.filter(pk=self.review.pk).update(status=Review.Status.IN_REVIEW)
        waiting = self.make_review(title='Do powiadomienia', status=Review.Status.ACCEPTED, decision_at=today)
        self.make_review(title='Powiadomiony', status=Review.Status.REJECTED, decision_at=today,
                         author_notified_at=today)
        planned = self.make_review(title='Zaplanowany', status=Review.Status.REJECTED, is_hidden=True,
                                   decision_at=today + timedelta(days=3))
        newest = self.make_review(title='Nowy')
        self.client.force_login(self.admin)
        url = reverse('core:review_list')
        for sort, expected in (('status', [newest.pk, self.review.pk]), ('-status', [self.review.pk, newest.pk])):
            with self.subTest(sort=sort):
                rows = self.client.get(url, {'sort': sort}).context['reviews']
                self.assertEqual([row['pk'] for row in rows], expected)
        queues = [('core:notification_queue', {}, [waiting.pk]),
                  ('core:notification_queue', {'q': 'POWIADOMIENIA'}, [waiting.pk]),
                  ('core:notification_queue', {'q': 'author@example'}, [waiting.pk]),
                  ('core:notification_queue', {'q': 'Zaplanowany'}, []),
                  ('core:scheduled_rejections', {}, [planned.pk])]
        for name, params, expected in queues:
            with self.subTest(name=name, **params):
                page = self.client.get(reverse(name), params)
                self.assertEqual([review.pk for review in page.context['reviews']], expected)
