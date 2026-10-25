"""Luki pokrycia interfejsu: sortowanie tabel, stronicowanie, powroty do list, wyszukiwanie i drobne formularze."""
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.db.models import F, Value
from django.http import HttpResponseRedirect
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from authors.models import Author
from core.author_access import contact_authors
from core.intake_forms import ExtractForm
from core.navigation import NavigationMiddleware, list_url, origin
from core.novel_forms import AssignmentForm, ChapterRangeForm
from core.pagination import get_page_size, paginate_items
from core.table_sorting import prepare_table_sort
from core.templatetags.workspace_tags import account_person
from core.translation_forms import TranslationForm
from fantazmaty.testing import create_member, create_superuser
from people.models import Person
from texts.models import Anthology, ForeignAuthor, Text


def sorted_request(sort, user=None):
    request = RequestFactory().get('/', {'sort': sort})
    request.user = user or AnonymousUser()
    return request


class QueryTableSortingTests(TestCase):
    def test_team_activity_without_action_column(self):
        member = create_member('aktywny', 'Redaktor')
        people = Person.objects.annotate(last_activity_at=Value(None))
        _, columns = prepare_table_sort(sorted_request('', member), people)
        self.assertEqual(columns['Data działania'], 'last_activity')
        self.assertNotIn('Działanie', columns)


class PaginationGapTests(TestCase):
    def test_page_size_rules(self):
        request = RequestFactory().get('/lista/')
        request.session = SessionStore()
        request.session['cms_page_sizes'] = {'/lista/|page_size|': 7}
        self.assertEqual(get_page_size(request), 25)
        with self.assertRaisesMessage(ValueError, 'Domyślny rozmiar strony'):
            get_page_size(request, default=7)

    def test_unordered_queryset_is_paginated_by_primary_key(self):
        for name in ('Bartek', 'Adam'):
            Person.objects.create(first_name=name, last_name='Strona')
        request = RequestFactory().get('/lista/')
        request.user = AnonymousUser()
        page = paginate_items(request, Person.objects.order_by())
        self.assertEqual([person.first_name for person in page], ['Bartek', 'Adam'])


class NavigationGapTests(SimpleTestCase):
    def test_relative_return_address_is_ignored(self):
        self.assertEqual(list_url(RequestFactory().get('/'), 'teksty/'), '')

    def test_form_on_list_returns_to_referring_list_and_keeps_other_list_redirect(self):
        request = RequestFactory().post('/recenzje/', HTTP_REFERER='http://testserver/teksty/?page=2')
        request.navigation_return = origin(request)
        self.assertEqual(request.navigation_return, '/teksty/?page=2')
        response = NavigationMiddleware(lambda request: None).process_response(
            request, HttpResponseRedirect('/recenzje/'))
        self.assertEqual(response.url, '/recenzje/')


class SearchGapTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('szukajacy', 'Redaktor')

    def setUp(self):
        self.client.force_login(self.member)

    def test_text_without_anthology_and_empty_query(self):
        Text.objects.create(title='Sierota bez zbioru', length=10)
        response = self.client.get(reverse('core:global_search'), {'q': 'sierota'})
        self.assertEqual([(row['title'], row['anthology']) for row in response.context['texts']],
                         [('Sierota bez zbioru', None)])
        response = self.client.get(reverse('core:global_search'), {'query': '   '})
        self.assertEqual((response.context['query'], response.context['texts']), ('', []))

    def test_polish_contains_compares_with_other_column(self):
        book = Anthology.objects.create(title='lodz')
        Text.objects.create(title='Wielka Łódź', anthology=book, length=10)
        Text.objects.create(title='Inny tytuł', anthology=book, length=10)
        found = Text.objects.filter(title__plcontains=F('anthology__title'))
        self.assertEqual([text.title for text in found], ['Wielka Łódź'])

    def test_unlinked_reviews_filtered_by_anthology(self):
        self.client.force_login(create_superuser('niepowiazane-admin'))
        first, second = (Anthology.objects.create(title=title) for title in ('Pierwsza', 'Druga'))
        for book in (first, second):
            Text.objects.create(title=f'Tekst {book.title}', anthology=book, length=10)
        response = self.client.get(reverse('core:unlinked_reviews'), {'anthology': str(first.pk)})
        self.assertContains(response, 'Tekst Pierwsza')
        self.assertNotContains(response, 'Tekst Druga')


class SmallFormGapTests(TestCase):
    def test_translation_form_accepts_plain_dict_data(self):
        author = ForeignAuthor.objects.create(first_name='Jan', last_name='Obcy')
        form = TranslationForm({'foreign_authors': str(author.pk)})
        self.assertEqual(list(form.fields['foreign_authors'].queryset), [author])

    def test_extract_author_without_email_requires_email(self):
        author = Author.objects.create(first_name='Ewa', last_name='Dawna', email=None)
        cases = {
            'autor bez e-maila': ({'author': author.pk}, 'Uzupełnij adres e-mail autora historycznego.'),
            'bez autora': ({}, None),
        }
        for name, (extra, message) in cases.items():
            with self.subTest(name):
                form = ExtractForm({'title': 'Miniatura', 'submission_dates': '2020', **extra})
                form.is_valid()
                if message:
                    self.assertEqual(form.errors['email'], [message])
                    self.assertEqual(form.cleaned_data['full_name'], 'Ewa Dawna')
                else:
                    self.assertNotIn('email', form.errors)

    def test_chapter_lengths_skip_empty_items(self):
        form = ChapterRangeForm({'chapter_numbers': '1-2', 'chapter_lengths': '1: 100,, 2: 200;'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['chapter_lengths'], {1: 100, 2: 200})

    def test_assignment_requires_person_eligible_for_role(self):
        proofreader = create_member('korektor-ui', 'Korektor')
        form = AssignmentForm({'role': 'editor', 'assignee': proofreader.pk})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['assignee'], ['Ta osoba nie może pełnić wybranej roli.'])
        empty = AssignmentForm({'role': '', 'assignee': ''})
        self.assertFalse(empty.is_valid())
        self.assertNotIn('__all__', empty.errors)

    def test_helpers_for_anonymous_user(self):
        Author.objects.create(first_name='Ukryty', last_name='Autor', email=None)
        self.assertIsNone(account_person(AnonymousUser()))
        self.assertEqual(list(contact_authors(AnonymousUser())), [])
