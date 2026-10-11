"""Paginacja tabel (zapamiętany rozmiar strony, skok do strony, parametry list) oraz kopiowanie
e-maili wszystkich przefiltrowanych wierszy."""
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from lxml import html

from core.models import Recruitment
from core.pagination import get_page_size, paginate_items
from fantazmaty.testing import create_superuser
from people.models import Person
from texts.models import Anthology


class PageSizePreferenceTests(SimpleTestCase):
    def request(self, path='/texts/', query=None, session=None):
        request = RequestFactory().get(path, query or {})
        request.session = session if session is not None else {}
        return request

    def test_return_navigation_retains_size_but_other_tables_are_independent(self):
        session = {}
        self.assertEqual(get_page_size(self.request(query={'page_size': 500}, session=session)), 500)
        self.assertEqual(get_page_size(self.request('/authors/', session=session)), 25)
        self.assertEqual(get_page_size(self.request(query={'q': 'smok', 'page': 2}, session=session)), 500)
        self.assertEqual(get_page_size(self.request(session=session), size_param='archive_page_size'), 25)
        self.assertEqual(get_page_size(self.request(query={'tab': 'unlinked'}, session=session)), 25)

    def test_invalid_values_cannot_replace_saved_preference(self):
        session = {}
        get_page_size(self.request(query={'page_size': 100}, session=session))
        for value in ('', '0', '-25', '26', '1e2', '500.0', '9' * 200):
            with self.subTest(value=value):
                self.assertEqual(get_page_size(self.request(query={'page_size': value}, session=session)), 100)
        self.assertEqual(get_page_size(self.request(query={'page_size': 50}, session=session)), 50)
        self.assertEqual(get_page_size(self.request(session=session)), 50)
        self.assertEqual(get_page_size(self.request(session={})), 25)

    def test_custom_paginator_retains_filters_anchor_and_bounds(self):
        session = {}
        query = {'archive_page_size': 50, 'roles': ['1', '2'], 'archive_page': 999, 'q': 'smoki', 'sort': '-title'}
        page = paginate_items(self.request('/author/2/', query, session), list(range(120)),
                              page_param='archive_page', size_param='archive_page_size', anchor='#archive')
        self.assertEqual(page.number, 3)
        self.assertIn('roles=1&roles=2', page.first_url)
        self.assertIn('archive_page=1', page.first_url)
        self.assertTrue(page.first_url.endswith('#archive'))
        returned = paginate_items(self.request('/author/2/', session=session), list(range(120)),
                                  page_param='archive_page', size_param='archive_page_size')
        self.assertEqual(returned.paginator.per_page, 50)
        self.assertEqual(returned.number, 1)


class PaginationNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser('pagination-coordinator', 'pagination@example.test')
        Anthology.objects.bulk_create([Anthology(title=f'Antologia {number:03}') for number in range(65)])

    def setUp(self):
        self.client.force_login(self.user)

    def assert_jump(self, response, current, maximum, param):
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content.decode('utf-8'))
        inputs = doc.xpath('//input[@data-server-page]')
        self.assertEqual(len(inputs), 1)
        field = inputs[0]
        self.assertEqual((field.get('value'), field.get('max'), field.get('data-page-param')),
                         (str(current), str(maximum), param))
        label = field.getparent()
        self.assertEqual(label.getprevious().text_content().strip(), 'Poprzednia')
        self.assertEqual(label.getnext().text_content().strip(), 'Następna')
        self.assertFalse(doc.xpath('//form//form'))

    def test_list_remembers_size_after_leaving_and_uses_current_page(self):
        url = reverse('core:anthology_list')
        self.assert_jump(self.client.get(url, {'page_size': 50, 'page': 2}), 2, 2, 'page')
        self.client.get(reverse('core:people_list'))
        response = self.client.get(url)
        self.assert_jump(response, 1, 2, 'page')
        self.assertEqual(response.context['page_obj'].paginator.per_page, 50)
        other_session = Client()
        other_session.force_login(self.user)
        self.assert_jump(other_session.get(url), 1, 3, 'page')

    def test_admin_record_list_remembers_size_and_preserves_native_page_parameter(self):
        url = reverse('admin:texts_anthology_changelist')
        self.assert_jump(self.client.get(url, {'page_size': 50, 'p': 2}), 2, 2, 'p')
        self.client.get(reverse('admin:index'))
        self.assert_jump(self.client.get(url), 1, 2, 'p')
        self.assert_jump(self.client.get(url, {'q': 'not-present'}), 1, 1, 'p')

    def test_unlinked_review_list_uses_shared_controls(self):
        response = self.client.get(reverse('core:data_integrity'), {'tab': 'unlinked', 'page_size': 100})
        self.assert_jump(response, 1, 1, 'page')
        self.assertContains(response, 'tab=unlinked')


class EmailCopyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('email-copy-admin', 'a@example.test')
        cls.member = get_user_model().objects.create_user('email-copy-member', 'm@example.test', 'test-only')
        Person.objects.create(user=cls.member, first_name='Członek', last_name='Zespołu', email='m@example.test')
        Recruitment.objects.bulk_create([
            Recruitment(first_name='Pasuje', last_name=str(i), email=f'p{i}@example.test') for i in range(510)])
        Recruitment.objects.create(first_name='Inny', last_name='Kandydat', email='excluded@example.test')

    def test_filtered_copy_pages_include_all_matches_without_changing_preference(self):
        self.client.force_login(self.admin)
        url = reverse('core:recruitment_list')
        self.client.get(url, {'page_size': 50})
        emails = set()
        for page, expected in [(1, 500), (2, 10)]:
            response = self.client.get(url, {'q': 'Pasuje', 'page_size': 500, 'page': page},
                                       HTTP_X_CMS_EMAIL_COPY='1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context['items']), expected)
            doc = html.fromstring(response.content)
            self.assertEqual(doc.xpath('string(//nav[@data-sort-config]/@data-result-count)'), '510')
            emails.update(item.email for item in response.context['items'])
        self.assertEqual(len(emails), 510)
        self.assertNotIn('excluded@example.test', emails)
        self.assertEqual(self.client.get(url).context['page_obj'].paginator.per_page, 50)

    def test_clipboard_control_only_for_superuser_and_theme_buttons_exist(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:recruitment_list'))
        self.assertContains(response, 'data-email-copy-toggle')
        self.assertContains(response, 'data-theme-autumn')
        self.assertContains(response, 'data-theme-toggle')
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:home'))
        self.assertNotContains(response, 'data-email-copy-toggle')
        self.assertNotContains(response, 'core/email-copy.js')
