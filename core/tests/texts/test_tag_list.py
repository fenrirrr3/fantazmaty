"""Lista tagów i gatunków tekstów: prezentowane dane i prywatność, wyszukiwanie, filtry, sortowanie
(także klucz polskiego sortowania w SQL produkcyjnego MySQL) i paginacja."""
from unittest.mock import patch

from django.db.backends.mysql.base import DatabaseWrapper as MySQLWrapper
from django.db.models import CharField, Value
from django.test import SimpleTestCase, TestCase, tag

from core.sort_keys import REPLACEMENTS
from core.views.tags import polish_key

from texts.models import Text

from .support import TaggedTextsFixture


class TagListTests(TaggedTextsFixture, TestCase):
    def ids(self, **params):
        response = self.client.get(self.list, params)
        self.assertEqual(response.status_code, 200)
        return [row.pk for row in response.context["page_obj"]]

    def test_list_metadata_is_live_and_private_contacts_are_not_exposed(self):
        response = self.client.get(self.list)
        for value in ("Antologia A", "Łąka", "Anna Żak", "Jan Adamczyk", "magia, podróż", "Fantasy"):
            self.assertContains(response, value)
        for value in (
            "private-author@example.test",
            "private-coauthor@example.test",
            "secret-source@example.test",
            "Ukryte Zgłoszenie",
        ):
            self.assertNotContains(response, value)
        self.text.genre = "Horror"
        self.text.save(update_fields=["genre"])
        self.assertContains(self.client.get(self.list), "Horror")
        self.assertNotContains(self.client.get(self.list), "Fantasy")

    def test_missing_genre_author_anthology_and_tags_render(self):
        response = self.client.get(self.list, {"anthology": "none"})
        for value in ("Zorza", "Brak danych", "Brak antologii", "Brak tagów"):
            self.assertContains(response, value)

    def test_search_each_displayed_field_and_full_author_name(self):
        for query in ("Łąka", "magia", "podróż", "Fantasy", "Jan Adamczyk", "Antologia A"):
            self.assertEqual(self.ids(q=query), [self.text.pk])

    @tag('slow')
    def test_filters_combine_and_include_coauthors(self):
        self.assertEqual(
            self.ids(anthology=str(self.book.pk), author=str(self.coauthor.pk), genre="Fantasy",
                     tag="mag", filled="yes"),
            [self.text.pk],
        )
        self.assertEqual(self.ids(anthology=str(self.book.pk), genre="Horror"), [])
        self.assertEqual(self.ids(anthology="none"), [self.orphan.pk])
        self.assertEqual(
            set(self.ids(genre="__missing__", filled="no")), {self.other.pk, self.orphan.pk}
        )
        for invalid in ("bad", "9" * 100, "-1", "0"):
            self.assertEqual(self.ids(anthology=invalid), [])
            self.assertEqual(self.ids(author=invalid), [])

    def test_text_genre_not_review_genre_is_filtered_searched_and_sorted(self):
        """Filtr, wyszukiwanie i sortowanie biorą gatunek tekstu, a nie gatunek recenzji źródłowej."""
        self.text.genre = "Groza"
        self.text.save(update_fields=["genre"])
        self.other.genre = "Fantasy"
        self.other.save(update_fields=["genre"])
        self.assertEqual(self.review.genre, "Fantasy")
        self.assertEqual(self.ids(genre="Groza"), [self.text.pk])
        self.assertEqual(self.ids(q="Groza"), [self.text.pk])
        ordered = self.ids(sort="genre")
        self.assertLess(ordered.index(self.other.pk), ordered.index(self.text.pk))

    @tag('slow')
    def test_sort_all_columns_both_directions_without_duplicate_rows(self):
        self.other.tags = "zebra"
        self.other.save(update_fields=["tags"])
        for key in ("title", "anthology", "author", "tags", "genre"):
            for prefix in ("", "-"):
                ids = self.ids(sort=prefix + key)
                self.assertEqual(len(ids), 3)
                self.assertEqual(set(ids), {self.text.pk, self.other.pk, self.orphan.pk})
        self.assertEqual(self.ids(sort="title"), [self.other.pk, self.text.pk, self.orphan.pk])
        self.assertEqual(self.ids(sort="author"), [self.text.pk, self.other.pk, self.orphan.pk])
        self.assertEqual(self.ids(sort="-tags")[0], self.other.pk)
        self.assertEqual(self.ids(sort="password"), self.ids(sort="title"))

    def test_sorting_is_before_pagination_and_filters_are_preserved(self):
        for n in range(28):
            Text.objects.create(title=f"Test {n:02}", anthology=self.book, length=1, tags="szukany")
        response = self.client.get(
            self.list, {"tag": "szukany", "sort": "-title", "page_size": 25, "page": 2}
        )
        page = response.context["page_obj"]
        self.assertEqual(page.paginator.count, 28)
        self.assertEqual([row.title for row in page], ["Test 02", "Test 01", "Test 00"])
        self.assertIn("tag=szukany", page.previous_url)
        self.assertIn("sort=-title", page.previous_url)

    def test_polish_sort_key_preserves_null_and_uppercase_order(self):
        row = (
            Text.objects.filter(pk=self.text.pk)
            .annotate(
                sample=Value(" ĄĆĘŁŃÓŚŹŻ ", output_field=CharField()),
                missing=Value(None, output_field=CharField()),
            )
            .annotate(
                key=polish_key("sample", Text.objects.db),
                empty_key=polish_key("missing", Text.objects.db),
            )
            .get()
        )
        self.assertEqual(row.key, "a~c~e~l~n~o~s~z~z~~")
        self.assertIsNone(row.empty_key)


class MySQLPolishSortKeyTests(SimpleTestCase):
    def test_key_lowercases_polish_capitals_and_converts_before_binary_collation(self):
        mysql = MySQLWrapper({'ENGINE': 'django.db.backends.mysql', 'NAME': '', 'OPTIONS': {}, 'TIME_ZONE': None},
                             alias='mysql')
        with patch('core.views.tags.connections', {'mysql': mysql}):
            key = polish_key('title', 'mysql')
        sql, params = Text.objects.annotate(key=key).values('key').query.get_compiler(connection=mysql).as_sql()
        self.assertTrue(sql.startswith('SELECT CONVERT(REPLACE('), sql)
        self.assertIn('TRIM(LOWER(REPLACE(', sql)
        self.assertIn('USING utf8mb4) COLLATE `utf8mb4_bin` AS `key`', sql)
        capitals = [value for letter, _ in REPLACEMENTS for value in (letter.upper(), letter)]
        keys = [value for pair in REPLACEMENTS for value in pair]
        self.assertEqual(list(params), capitals + keys)
