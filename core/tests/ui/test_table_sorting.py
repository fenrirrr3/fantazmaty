"""Sortowanie tabel po stronie serwera: kontrakt „każda kolumna danych ma sortowanie” dla wszystkich
stronicowanych list oraz zasady sortowania (braki na końcu, prywatne kolumny, kolumny wyliczane)."""
import json
import re
from datetime import timedelta
from html import unescape

from django.http import QueryDict
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.models import Recruitment, WorkflowEvent
from core.pagination import paginate_items
from core.selectors.texts import _ProjectedRows, _stage_data, my_texts_context, text_list_context, workflow_list_context
from core.table_sorting import prepare_table_sort
from fantazmaty.testing.data import CoreTestDataMixin
from illustrations.models import Illustrator
from people.models import Person, Role
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage

# Trasy ze stronicowanymi tabelami; „my_reviews” ogląda recenzent, resztę superużytkownik.
# Listy tekstów i etapów budują rozbudowane zapytania procesu, więc mają osobną klasę testów
# (przy --parallel obie części kontraktu wykonują się równolegle).
TEXT_TABLE_ROUTES = ('core:text_list', 'core:my_texts', 'core:available_texts', 'core:workflow_list')
SORTABLE_ROUTES = TEXT_TABLE_ROUTES + (
    'core:review_list', 'core:my_reviews', 'core:author_list', 'core:people_list',
    'core:anthology_list', 'core:recruitment_list', 'core:extract_list',
    'core:anthology_corrections', 'core:user_activity', 'core:notification_queue',
    'core:scheduled_rejections', 'core:active_vacations', 'core:audiobooks', 'core:novel_list',
    'core:task_list', 'core:tag_list', 'core:vocabulary_list', 'core:workflow_inactivity',
    'illustrations:illustrator_list', 'illustrations:illustration_list',
    'illustrations:cover_proposal_list',
)
# Nagłówki kolumn bez danych (akcje, wybór wierszy), których nie sortuje się.
NON_DATA_HEADERS = {'Akcje', 'Akcja', 'Szczegóły', 'Powiadomienie', 'Wybór', 'Porządkowanie',
                    'Zmień status'}
SORT_CONFIG = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>\s*(<nav[^>]*data-sort-config[^>]*>)', re.S)


def sort_request(sort, user):
    request = RequestFactory().get('/', {'sort': sort})
    request.user = user
    return request


class SortableTablesContractData(CoreTestDataMixin):
    """Dane i sprawdzenie kontraktu: każda kolumna danych ma sortowanie serwera w obu kierunkach."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        text = Text.objects.create(title='Sortowanie', anthology=cls.anthology, length=10)
        text.authors.add(cls.author)
        cls.anthology.has_illustrations = True
        cls.anthology.save()
        assignment = WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=cls.editor)
        WorkflowStage.objects.create(text=text, stage_type='editing', assignment=assignment,
                                     started_at=timezone.localdate())
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        novel.novel.authors.add(cls.author)
        Illustrator.objects.create(first_name='Artysta')

    def assert_every_data_header_sorts_in_both_directions(self, routes):
        for route in routes:
            self.client.force_login(self.reviewer if route == 'core:my_reviews' else self.superuser)
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                page = response.content.decode()
                configs = SORT_CONFIG.findall(page)
                self.assertTrue(configs, 'Brak konfiguracji sortowania tabeli')
                columns = {}
                for config, _ in configs:
                    columns.update(json.loads(config) or {})
                for head in re.findall(r'<th\b[^>]*>(.*?)</th>', page, re.S):
                    label = unescape(re.sub('<[^>]+>', '', head)).strip()
                    if label and label not in NON_DATA_HEADERS:
                        self.assertIn(label, columns)
                # Każde zadeklarowane sortowanie, także ukryte za warunkami szablonu.
                for config, nav in configs:
                    param = re.search(r'data-sort-param="([^"]+)"', nav)
                    param = param[1] if param else 'sort'
                    for key in set((json.loads(config) or {}).values()):
                        for direction in ('', '-'):
                            with self.subTest(sort=direction + key):
                                result = self.client.get(reverse(route), {param: direction + key})
                                self.assertEqual(result.status_code, 200)


class TextTablesSortContractTests(SortableTablesContractData, TestCase):
    @tag('slow')
    def test_text_and_stage_tables_sort_every_data_header_in_both_directions(self):
        self.assert_every_data_header_sorts_in_both_directions(TEXT_TABLE_ROUTES)

    @tag('slow')
    def test_computed_workflow_columns_sort_without_losing_rows(self):
        for context, key in ((workflow_list_context(user=self.superuser, params=QueryDict()), 'stages'),
                             (my_texts_context(user=self.editor, params=QueryDict()), 'texts')):
            rows = context[key]
            _, columns = prepare_table_sort(sort_request('', self.superuser), rows)
            for sort in set(columns.values()):
                with self.subTest(sort=sort):
                    result, _ = prepare_table_sort(sort_request(sort, self.superuser), rows)
                    self.assertEqual(len(list(result)), len(list(rows)))


class OtherTablesSortContractTests(SortableTablesContractData, TestCase):
    @tag('slow')
    def test_other_tables_sort_every_data_header_in_both_directions(self):
        self.assert_every_data_header_sorts_in_both_directions(
            [route for route in SORTABLE_ROUTES if route not in TEXT_TABLE_ROUTES])


class TableSortingTests(CoreTestDataMixin, TestCase):
    def request(self, sort, user=None):
        return sort_request(sort, user or self.superuser)

    def test_sort_covers_all_pages_and_preserves_values_projection(self):
        Recruitment.objects.bulk_create([
            Recruitment(first_name=str(i), last_name='Osoba', email=f'{i}@example.com', notes=f'{39-i:02}')
            for i in range(40)])
        page = paginate_items(self.request('notes'), Recruitment.objects.all())
        self.assertEqual(page.paginator.count, 40)
        self.assertEqual(page.object_list[0].notes, '00')
        page = paginate_items(self.request('-notes'), Recruitment.objects.all())
        self.assertEqual(page.object_list[0].notes, '39')

    def test_roles_do_not_duplicate_people(self):
        role, _ = Role.objects.get_or_create(name='Grafik')
        self.editor_person.roles.add(role)
        items, _ = prepare_table_sort(self.request('roles'), Person.objects.prefetch_related('roles'))
        self.assertEqual(len(items), Person.objects.count())
        self.assertEqual(len({p.pk for p in items}), len(items))

    def test_author_anthologies_support_dict_queryset(self):
        text = Text.objects.create(title='T', anthology=self.anthology, length=1)
        text.authors.add(self.author)
        empty = Author.objects.create(first_name='Bez', last_name='Tekstów', email='empty@example.com')
        for key in ('anthologies', '-anthologies'):
            items, _ = prepare_table_sort(self.request(key), Author.objects.values('pk', 'first_name'))
            self.assertEqual(items[0]['pk'], self.author.pk)
            self.assertEqual(items[-1]['pk'], empty.pk)

    def test_private_sort_is_not_advertised_and_raw_orm_is_ignored(self):
        review = Review.objects.create(title='T', anthology=self.anthology, length=1)
        for key in ('author', 'email', 'author__notes__content'):
            items, columns = prepare_table_sort(self.request(key, self.editor), Review.objects.all())
            self.assertNotIn('Autor', columns)
            self.assertEqual(list(items.values_list('pk', flat=True)), [review.pk])

    def test_missing_values_last_in_both_directions(self):
        rows = [{'title': 'Łódź'}, {'title': ''}, {'title': 'Las'}, {'title': None}, {'title': 'Żar'}]
        for sort, expected in (
            ('title', ['Las', 'Łódź', 'Żar', '', None]),
            ('-title', ['Żar', 'Łódź', 'Las', '', None]),
        ):
            with self.subTest(sort=sort):
                items, _ = prepare_table_sort(self.request(sort), rows)
                self.assertEqual([row['title'] for row in items], expected)

    def test_review_counts_sort_only_submitted_opinions(self):
        first = Review.objects.create(title='A', anthology=self.anthology, length=1)
        second = Review.objects.create(title='B', anthology=self.anthology, length=1)
        ReviewAssignment.objects.create(review=first, user=self.reviewer, position=1, opinion='reading')
        ReviewAssignment.objects.create(review=second, user=self.reviewer, position=1, opinion='yes')
        rows, _ = prepare_table_sort(self.request('-opinions'), Review.objects.all())
        self.assertEqual(list(rows.values_list('pk', flat=True)), [second.pk, first.pk])

    def test_status_date_excludes_non_status_events_and_sorts_before_pagination(self):
        user = self.superuser
        one = Text.objects.create(title="A", length=100)
        two = Text.objects.create(title="B", length=100)
        missing = Text.objects.create(title="C", length=100)

        def event(text, previous, next_):
            return WorkflowEvent.objects.create(text=text, title=text.title, actor=user, actor_name="A",
                                                previous_status=previous, next_status=next_, channel="")

        first = event(one, "Redakcja", "Weryfikacja")
        second = event(two, "Redakcja", "Korekta")
        # Kolejne zapisy mogą mieć ten sam znacznik czasu (zwłaszcza w Windows) – ustawiamy je jawnie.
        first.created_at = timezone.now() - timedelta(minutes=2)
        second.created_at = first.created_at + timedelta(minutes=1)
        WorkflowEvent.objects.filter(pk=first.pk).update(created_at=first.created_at)
        WorkflowEvent.objects.filter(pk=second.pk).update(created_at=second.created_at)
        event(one, "Weryfikacja", "Weryfikacja")
        for direction, expected in [("-", [two.pk, one.pk, missing.pk]), ("", [one.pk, two.pk, missing.pk])]:
            request = self.request(direction + "last_status_change", user)
            rows = text_list_context(user=user, params=request.GET)["texts"]
            rows, columns = prepare_table_sort(request, rows)
            self.assertEqual([r["pk"] for r in rows], expected)
            self.assertEqual(columns["Ostatnia zmiana statusu"], "last_status_change")
            values = {r["pk"]: r["last_status_change"] for r in rows}
            self.assertEqual(values[one.pk], first.created_at)
            self.assertEqual(values[two.pk], second.created_at)

    def test_stage_state_column_names_and_orders_each_state(self):
        """Kolumna „Stan etapu” listy etapów: zakończony, zaplanowany, w trakcie i nierozpoczęty, w obu kierunkach."""
        text = Text.objects.create(title='Stany', anthology=self.anthology, length=1)
        today = timezone.localdate()
        for kind, values in (
            ('editing', {'started_at': today - timedelta(days=1)}),
            ('first_verification', {'started_at': today + timedelta(days=3)}),
            ('first_proofreading', {}),
            ('second_proofreading', {'started_at': today, 'ended_at': today, 'is_completed': True}),
        ):
            WorkflowStage.objects.create(text=text, stage_type=kind, **values)
        rows = _ProjectedRows(WorkflowStage.objects.filter(text=text, is_current=True), _stage_data)
        expected = ['first_proofreading', 'editing', 'second_proofreading', 'first_verification']
        for sort, order in (('completed', expected), ('-completed', expected[::-1])):
            with self.subTest(sort=sort):
                items, columns = prepare_table_sort(self.request(sort), rows)
                self.assertEqual(columns['Stan etapu'], 'completed')
                self.assertEqual([row['stage_type'] for row in items], order)

    def test_mobile_sort_can_return_to_default(self):
        rendered = render_to_string("core/includes/mobile_table_sort.html", {
            "request": RequestFactory().get("/moje-teksty/", {"sort": "title"}),
            "mobile_sort_columns": [("Tytuł", "title")],
        })
        self.assertIn('<option value="">Kolejność domyślna</option>', rendered)
        self.assertIn('value="title" selected', rendered)
