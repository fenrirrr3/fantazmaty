"""Widoczność tłumaczeń: lista i szczegóły tłumaczeń, wyłączenie z list zwykłych tekstów, praca osobista i nawigacja."""
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.selectors.texts import available_stages_for_user, my_texts_context, user_workflow_summary
from fantazmaty.testing import form_value
from texts.models import Anthology
from workflow.models import WorkflowRoleAssignment

from .support import CompletedTranslationData, TranslatedBookData


def main_table(sql):
    """Główna tabela zapytania SELECT (pomija podzapytania w nawiasach); SQLite i MySQL."""
    depth = 0
    for index, char in enumerate(sql):
        depth += {'(': 1, ')': -1}.get(char, 0)
        if depth == 0 and sql.startswith(' FROM ', index):
            return sql[index + 6:].split(None, 1)[0].strip('`"')
    return None


class TranslationVisibilityTests(TranslatedBookData, TestCase):
    def test_translation_list_same_columns_search_filter_sort_and_empty_anthology(self):
        blank = Anthology.objects.create(title="Puste tłumaczenie", is_translated=True)
        url = reverse("core:translation_list")
        for params in (
            {},
            {"q": "Żak"},
            {"translator": str(self.translator.pk)},
            {"anthology": str(self.book.pk)},
            {"sort": "translators"},
            {"sort": "-length"},
            {"status": "editing"},
        ):
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r["pk"] for r in response.context["texts"]], [self.text.pk])
            self.assertContains(response, "Anna Żak")
            for heading in ["Antologia", "Tytuł", "Autorzy", "Tłumacz", "Długość", "Etap pracy",
                            "Rozpoczęcie etapu", "Ostatnia zmiana statusu"]:
                self.assertContains(response, heading)
        response = self.client.get(url, {"anthology": blank.pk})
        self.assertContains(response, "Puste tłumaczenie")
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        response = self.client.get(url, {"anthology": self.normal.pk})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_ordinary_lists_search_and_filters_do_not_expose_translation_records(self):
        """Błąd: teksty, antologie i recenzje tłumaczeń przeciekały do zwykłych list i wyszukiwań."""
        routes = ["tag_list", "review_list", "workflow_list", "home", "my_texts", "anthology_corrections",
                  "data_integrity", "author_list"]
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(reverse("core:" + route), {"hide_ready": "0", "q": "TRANSLATED UNIQUE"})
                self.assertEqual(response.status_code, 200)
                # A search input may echo q; inspect complete title strings instead.
                self.assertNotContains(response, self.text.title)
                self.assertNotContains(response, self.book.title)
                self.assertNotContains(response, self.review.title)
        response = self.client.get(reverse("core:text_list"), {"anthology": self.book.pk, "hide_ready": "0"})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(response, self.book.title)
        response = self.client.get(reverse("illustrations:illustration_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.book.title)

    def test_home_workload_and_person_profile_include_translations(self):
        summary = user_workflow_summary(self.member)
        self.assertEqual(summary["active_stage_count"], 1)
        self.assertEqual(summary["reserved_assignment_count"], 0)
        rows = available_stages_for_user(user=self.admin)
        self.assertNotIn(self.stage.pk, [r.pk for r in rows])
        response = self.client.get(reverse("core:person_detail", args=[self.member.person_profile.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.book.title)

    def test_member_sees_names_but_no_private_contacts_and_cannot_edit_translators(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("core:translation_list"))
        self.assertContains(response, "Anna Żak")
        self.assertNotContains(response, "translator-private@example.test")
        response = self.client.get(reverse("core:translation_detail", args=[self.text.pk]))
        self.assertContains(response, "Anna Żak")
        self.assertNotContains(response, "translator-private@example.test")
        response = self.client.post(reverse("core:set_translators", args=[self.text.pk]),
                                    {"translators": [self.other.pk]})
        self.assertEqual(response.status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core:translation_list")).status_code, 302)

    def test_detail_page_reads_text_and_translators_without_repeating_selector_queries(self):
        """Budowa strony tłumaczenia: tekst pobiera tylko selektor (widok nie pobiera go ponownie przy GET),
        tłumaczy – też tylko selektor, a przydział osoby widok bierze z selektora (bez osobnego zapytania)."""
        from core.views import texts as text_views
        page = text_views._render_text_detail_page
        captured = []

        def spy(*args, **kwargs):
            with CaptureQueriesContext(connection) as queries:
                response = page(*args, **kwargs)
            captured.extend(main_table(query['sql']) for query in queries.captured_queries)
            return response

        self.client.force_login(self.member)
        with patch('core.views.texts._render_text_detail_page', spy):
            response = self.client.get(reverse("core:translation_detail", args=[self.text.pk]))
        self.assertContains(response, "Anna Żak")
        self.assertEqual({table: captured.count(table) for table in
                          ('texts_text', 'texts_translator', 'workflow_workflowroleassignment')},
                         {'texts_text': 1, 'texts_translator': 1, 'workflow_workflowroleassignment': 2})
        self.assertTrue(response.context['is_assigned'])
        self.assertTrue(response.context['can_add_note'])

    def test_regular_details_redirect_and_translation_route_rejects_normal_text(self):
        response = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
        self.assertRedirects(response, reverse("core:translation_detail", args=[self.text.pk]))
        self.assertEqual(self.client.get(reverse("core:translation_detail", args=[self.regular.pk])).status_code,
                         404)
        response = self.client.get(reverse("core:anthology_detail", args=[self.book.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zadania antologii")

    def test_translation_person_buttons_in_one_action_group(self):
        page = self.client.get(reverse('core:translation_detail', args=[self.text.pk]))
        self.assertEqual(page.status_code, 200)
        group = html.fromstring(page.content).xpath('//div[contains(@class, "translation-person-actions")]')[0]
        self.assertEqual(len(group.xpath('./a[contains(@class,"secondary-button")]')), 2)
        self.assertEqual(group[-1].text_content(), 'Zapisz osoby')

    def test_admin_flag_and_translator_inline_only_for_translated_text(self):
        response = self.client.get(reverse("admin:texts_anthology_change", args=[self.book.pk]))
        self.assertContains(response, 'name="is_translated"')
        response = self.client.get(reverse("admin:texts_text_change", args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "translation-0-translators")
        response = self.client.get(reverse("admin:texts_text_change", args=[self.regular.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "translation-0-translators")

    def test_translations_in_all_tasks_personal_and_dashboard_but_not_ordinary_workflow(self):
        self.client.force_login(self.member)
        for route in ('text_list', 'my_texts', 'home'):
            response = self.client.get(reverse('core:' + route))
            self.assertContains(response, self.text.title)
        response = self.client.get(reverse('core:dashboard_tasks'), {'kind': 'active'})
        self.assertIn(self.text.title, response.json()['html'])
        response = self.client.get(reverse('core:workflow_list'))
        self.assertNotContains(response, self.text.title)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('core:task_list'))
        self.assertContains(response, self.book.title)
        self.assertContains(response, reverse('core:anthology_detail', args=[self.book.pk]) + '#task-cover')
        response = self.client.get(reverse('core:anthology_detail', args=[self.book.pk]))
        self.assertContains(response, 'id="task-cover"')
        self.assertContains(response, 'id="task-blurb"')
        self.assertContains(response, reverse('core:translation_list') + '?anthology=')

    def test_return_through_translation_redirect_preserves_filters_and_active_menu(self):
        self.client.force_login(self.member)
        back = reverse('core:my_texts') + '?view=all&page=4&page_size=50&q=Podróż'
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]),
                                   HTTP_REFERER='http://testserver' + back, follow=True)
        self.assertEqual(response.status_code, 200)
        query = urlsplit(response.request['PATH_INFO'] + '?' + response.request['QUERY_STRING']).query
        self.assertEqual(parse_qs(query)['_back'][0],
                         '/moje-teksty/?view=all&page=4&page_size=50&q=Podr%C3%B3%C5%BC')
        doc = html.fromstring(response.content)
        active = doc.xpath('//a[contains(@class,"sidebar-link")][@aria-current="page"]/@href')
        self.assertEqual(active, [reverse('core:my_texts')])
        self.assertTrue(doc.xpath('//a[@href="' + response.context['return_url'] + '"]'))

    def test_coordinator_saves_translated_book_tasks_without_admin_and_returns_to_filtered_tasks(self):
        self.client.force_login(self.manager)
        url = reverse('core:anthology_detail', args=[self.book.pk])
        back = '/zadania/?q=Przekłady&page=2'
        response = self.client.get(url, {'_back': back})
        data = {'_edit_version': form_value(response), '_back': back}
        for form in response.context['task_forms']:
            data[form.prefix + '-status'] = form['status'].value()
            data[form.prefix + '-assigned_to'] = form['assigned_to'].value() or ''
        data.update({'blurb-status': 'commissioned', 'blurb-assigned_to': self.member.person_profile.pk})
        saved = self.client.post(url, data)
        self.assertEqual(saved.status_code, 302)
        self.assertIn('_back=', saved.url)
        task = self.book.production_tasks.get(task_type='blurb')
        self.assertEqual(task.status, 'commissioned')
        self.assertEqual(task.assigned_to_id, self.member.person_profile.pk)
        self.assertEqual(task.commissioned_at, timezone.localdate())
        self.assertNotIn('Brak autora', str(response.context['issue_groups']))


class CompletedTranslationTests(CompletedTranslationData, TestCase):
    def test_completed_translation_is_in_personal_all_and_profile_with_executions(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:my_texts'), {'view': 'all'})
        self.assertContains(response, self.text.title)
        self.assertContains(response, self.normal.title)
        self.assertContains(response, 'JZ')
        self.assertNotContains(response, 'Jane Żak')
        row = next(row for row in response.context['texts'] if row['pk'] == self.text.pk)
        self.assertEqual(len(row['user_assignments']), 2)
        self.assertTrue(row['has_completed_work'])
        self.assertFalse(row['has_active_work'] or row['has_reserved_work'])
        response = self.client.get(reverse('core:my_texts'), {'view': 'active'})
        self.assertNotContains(response, self.text.title)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:person_detail', args=[self.member.person_profile.pk]))
        self.assertContains(response, self.text.title)
        self.assertContains(response, reverse('core:translation_person_detail', args=['author', self.foreign.pk]))
        self.assertContains(response, reverse('core:translation_detail', args=[self.text.pk]))
        self.assertEqual(response.context['person_summary']['completed'], 2)

    def test_personal_filters_include_translated_anthology_and_keep_author_ids_separate(self):
        # Give the admin work too so the author filter is available in a personal list.
        for text in (self.text, self.normal):
            WorkflowRoleAssignment.objects.create(text=text, role='proofreader_1', assigned_to=self.admin)

        def texts(query):
            result = my_texts_context(user=self.admin, selected_view='all', params=QueryDict(query))
            return result, [row['pk'] for row in result['texts']]

        for value, expected in ((str(self.domestic.pk), self.normal.pk), ('foreign:' + str(self.foreign.pk),
                                                                          self.text.pk)):
            self.assertEqual(texts('author=' + value)[1], [expected])
        result, found = texts('anthology=' + str(self.book.pk))
        self.assertEqual(found, [self.text.pk])
        self.assertIn(self.book.pk, [row['pk'] for row in result['anthologies']])
        self.assertEqual(list(texts('q=Jane')[0]['texts']), [])
        self.assertEqual(texts('q=JZ')[1], [self.text.pk])

    def test_translation_defaults_show_ready_and_aggregate_lists_still_exclude(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:translation_list'))
        self.assertContains(response, self.text.title)
        self.assertFalse(response.context['hide_ready'])
        self.assertFalse(html.fromstring(response.content).xpath('//input[@id="text-hide-ready"]/@checked'))
        response = self.client.get(reverse('core:translation_list'), {'hide_ready': '1'})
        self.assertNotContains(response, self.text.title)
        response = self.client.get(reverse('core:text_list'), {'hide_ready': '0'})
        self.assertContains(response, self.text.title)
        response = self.client.get(reverse('core:workflow_list'), {'hide_ready': '0'})
        self.assertContains(response, self.normal.title)
        self.assertNotContains(response, self.text.title)
        self.assertNotContains(response, self.book.title)
