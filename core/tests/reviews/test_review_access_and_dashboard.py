"""Dostęp do recenzji według ról (menu, lista, pojedyncze zgłoszenia, wyszukiwarka) i recenzje na pulpicie."""
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.views.search import _search_reviews
from fantazmaty.testing import create_member, create_superuser
from people.models import Role
from texts.models import Anthology, Review, ReviewAssignment, Text


class ReviewAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('v84editor', 'Redaktor')
        cls.reviewer = create_member('v84reviewer', 'Recenzent')
        cls.book = Anthology.objects.create(title='Recenzje v84')
        common = dict(anthology=cls.book, author_first_name='A', author_last_name='B', email='a@example.test',
                      length=10)
        cls.accepted = Review.objects.create(title='Szukane przyjęte', status='accepted', **common)
        cls.rejected = Review.objects.create(title='Szukane odrzucone', status='rejected', **common)
        cls.withdrawn = Review.objects.create(title='Szukane wycofane', status='withdrawn', **common)
        cls.open = Review.objects.create(title='Szukane nowe', status='new', **common)

    def test_member_without_reviewer_role_has_no_review_menu_but_opens_single_reviews(self):
        """Lista Recenzje jest dla recenzentów; redaktor otwiera tylko pojedyncze recenzje (np. z tekstu)."""
        text = Text.objects.create(title='Tekst z recenzją', anthology=self.book, length=10)
        linked = Review.objects.create(title='Tekst z recenzją', status='accepted', anthology=self.book,
                                       author_first_name='A', author_last_name='B', email='a@example.test',
                                       length=10, copied_text=text)
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(reverse('core:home')), 'data-menu-section="submissions"')
        self.assertEqual(self.client.get(reverse('core:review_list')).status_code, 403)
        detail = self.client.get(reverse('core:assigned_text_detail', args=[text.pk]))
        self.assertContains(detail, reverse('core:assigned_review_detail', args=[linked.pk]))
        review = self.client.get(reverse('core:assigned_review_detail', args=[linked.pk]))
        self.assertEqual(review.status_code, 200)
        self.assertContains(review, 'Wróć do tekstu')
        self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[self.withdrawn.pk])).status_code,
                         404)
        self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[self.accepted.pk])).status_code,
                         200)
        found = {row['title'] for row in _search_reviews('Szukane', user=self.editor, include_authors=False)}
        self.assertEqual(found, {'Szukane przyjęte', 'Szukane odrzucone', 'Szukane nowe'})

    def test_reviewer_sees_menu_current_list_and_decisions_without_withdrawn(self):
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(reverse('core:home')), 'data-menu-section="submissions"')
        current = self.client.get(reverse('core:review_list'))
        self.assertEqual(current.status_code, 200)
        self.assertEqual({row['title'] for row in current.context['page_obj']}, {'Szukane nowe'})
        archive = self.client.get(reverse('core:review_list'), {'old_reviews': '1'})
        self.assertEqual({row['title'] for row in archive.context['page_obj']},
                         {'Szukane przyjęte', 'Szukane odrzucone'})

    def test_coordinator_gets_author_profile_links_on_reviews(self):
        from core.views.reviews import _permission_context

        coordinator = create_member('coord13', 'Koordynator redakcji')
        self.assertTrue(_permission_context(coordinator)['can_open_author_profiles'])


class ReviewDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('layout-admin')
        cls.member = create_member('layout-member', 'Recenzent')
        cls.member.person_profile.roles.add(Role.objects.get_or_create(name='Redaktor')[0])
        cls.book = Anthology.objects.create(title='Antologia')

    def test_pending_reviews_include_multi_role_user_and_paginate_without_other_users(self):
        self.client.force_login(self.member)
        for n in range(8):
            review = Review.objects.create(title=f'Oczekuje {n}', anthology=self.book, length=100)
            ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion='reading')
        for title, kwargs, opinion in [
            ('Oddana', {}, 'yes'),
            ('Archiwum', {'old_reviews': True}, 'reading'),
            ('Ukryta', {'is_hidden': True}, 'reading'),
            ('Zamknięta', {'status': 'accepted'}, 'reading'),
        ]:
            review = Review.objects.create(title=title, anthology=self.book, length=100, **kwargs)
            ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion=opinion)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.context['pending_review_count'], 8)
        self.assertEqual(len(response.context['pending_reviews']), 6)
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[contains(@class,"dashboard-right-stack")]/section')), 2)
        second = self.client.get(reverse('core:dashboard_tasks'), {'kind': 'reviews', 'page': 2}).json()
        self.assertEqual(second['html'].count('dashboard-list-item'), 2)
        self.assertIsNone(second['next_url'])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('core:home')).context['pending_review_count'], 0)

    def test_admin_dashboard_intake_pair_buttons_and_program_instructions(self):
        self.client.force_login(self.admin)
        doc = html.fromstring(self.client.get(reverse('core:home')).content)
        headings = doc.xpath('//*[contains(@class,"dashboard-intake-grid")]/section/h2/text()')
        self.assertEqual(len(headings), 2)
        links = doc.xpath('//a[contains(text(), "Obsłuż zgłoszenia")]/parent::div/a')
        self.assertEqual(len(links), 2)
        self.assertTrue(all('button' in link.attrib['class'] for link in links))
        self.assertNotContains(self.client.get(reverse('core:author_list')), 'brak wyboru')
        page = self.client.get(reverse('core:programs'))
        self.assertEqual(page.status_code, 200)
        for phrase in ('uruchamiamy', 'zachowujemy', 'ustawiamy', 'pomijamy', 'przenosimy', 'poprawiamy'):
            self.assertNotContains(page, phrase)
