"""Raporty: zegary bezczynności, strony przestojów i aktywności zespołu (filtry, uprawnienia, dane autorów),
ostatnia aktywność osób, zapytania list tekstów i kontrola wielokrotnych przypisań."""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.assignment_integrity import assignment_conflicts, assignment_conflict_details
from core.edit_versions import version_of
from core.selectors.reports import workflow_inactivity_context
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from people.models import Person
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.admin_performers import correct_stage_performers
from workflow.handoffs import handoff_stage
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S, WorkflowRepetition
from workflow.services import send_to_first_verification
from workflow.waiting import CLOCK_FIELDS, annotate_clock_terms, inactivity_since

from .support import AnthologyWorkflowMixin, add_roles, importing


class WaitingClockTests(AnthologyWorkflowMixin, TestCase):
    """Od kiedy etap czeka lub trwa w raporcie bezczynności (rezerwacje, przekazania, poprawki)."""

    def clock(self, stage):
        """Zegary etapu tak, jak liczy je raport przestojów."""
        terms = dict(zip(CLOCK_FIELDS, annotate_clock_terms(S.objects.filter(pk=stage.pk)).values_list(
            *CLOCK_FIELDS).get()))
        waiting, active = inactivity_since(*terms.values(), today=self.today)
        return SimpleNamespace(report_blocked=terms['clock_blocked'], report_waiting_since=waiting,
                               report_active_since=active)

    def test_early_reservation_is_zero_before_and_after_actual_handoff(self):
        self.begin_editing()
        first = self.stage("first_verification")
        S.objects.filter(pk=first.pk).update(queued_at=self.today - timedelta(days=40))
        row = self.clock(first)
        self.assertTrue(row.report_blocked)
        self.assertEqual(row.report_waiting_since, self.today)
        send_to_first_verification(self.text, self.editor, self.today)
        row = self.clock(first)
        self.assertFalse(row.report_blocked)
        self.assertEqual(row.report_waiting_since, self.today)
        self.assertFalse(
            workflow_inactivity_context(user=self.superuser, params=QueryDict("mode=waiting"))[
                "rows"
            ]
        )

    def test_legacy_reservation_uses_relevant_completion_not_unrelated_activity(self):
        editing = self.begin_editing()
        first = self.stage("first_verification")
        S.objects.filter(pk=first.pk).update(queued_at=self.today - timedelta(days=40))
        S.objects.filter(pk=editing.pk).update(
            started_at=self.today - timedelta(days=35),
            ended_at=self.today - timedelta(days=20),
            is_completed=True,
        )
        S.objects.create(
            text=self.text,
            stage_type="styling",
            started_at=self.today,
            ended_at=self.today,
            is_completed=True,
        )
        self.assertEqual(self.clock(first).report_waiting_since, self.today - timedelta(days=20))

    def test_unknown_predecessor_date_remains_unknown(self):
        self.initial_stage.delete()
        with importing():
            S.objects.create(
                text=self.text, stage_type="editing", imported_completed=True, is_completed=True
            )
        first = S.objects.create(
            text=self.text,
            stage_type="first_verification",
            queued_at=self.today - timedelta(days=40),
        )
        self.assertIsNone(self.clock(first).report_waiting_since)
        rows = workflow_inactivity_context(user=self.superuser, params=QueryDict("mode=waiting"))[
            "rows"
        ]
        self.assertIsNone(rows[0]["days"])

    def test_handoff_resets_waiting_without_rewriting_original_work_dates(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        stage = S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=assignment,
            queued_at=self.today - timedelta(days=40),
            started_at=self.today - timedelta(days=30),
        )
        handoff_stage(
            self.text,
            self.superuser,
            stage_id=stage.pk,
            assigned_to_id=self.other_editor.pk,
            expected_assignment_id=assignment.pk,
            reason="Przekazanie",
        )
        stage.refresh_from_db()
        self.assertEqual(stage.waiting_reset_at, self.today)
        self.assertEqual(self.clock(stage).report_waiting_since, self.today)
        self.assertEqual(stage.handoffs.get().original_started_at, self.today - timedelta(days=30))

    def test_manual_correction_resets_only_changed_unfinished_work(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        stage = S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=assignment,
            queued_at=self.today - timedelta(days=40),
            started_at=self.today - timedelta(days=30),
        )
        done = S.objects.create(
            text=self.text,
            stage_type="editing",
            iteration=2,
            assignment=assignment,
            started_at=self.today - timedelta(days=35),
            ended_at=self.today - timedelta(days=31),
            is_completed=True,
        )
        correct_stage_performers(
            self.text.pk, {stage.pk: self.editor}, self.superuser, version_of(self.text)
        )
        stage.refresh_from_db()
        self.assertIsNone(stage.waiting_reset_at)
        correct_stage_performers(
            self.text.pk, {stage.pk: self.other_editor}, self.superuser, version_of(self.text)
        )
        stage.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(stage.waiting_reset_at, self.today)
        self.assertEqual(stage.started_at, self.today - timedelta(days=30))
        self.assertEqual(self.clock(stage).report_active_since, self.today)
        self.assertIsNone(done.waiting_reset_at)

    def test_assignment_save_resets_clock_only_on_real_performer_change(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        stage = S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=assignment,
            queued_at=self.today - timedelta(days=40),
        )
        assignment.notes = "Notatka"
        assignment.save(update_fields=["notes"])
        stage.refresh_from_db()
        self.assertIsNone(stage.waiting_reset_at)
        assignment.assigned_to = self.other_editor
        assignment.save(update_fields=["assigned_to"])
        stage.refresh_from_db()
        self.assertEqual(stage.waiting_reset_at, self.today)


class ReportTests(StatusAssignmentFixtures):
    """Raporty aktywności i listy tekstów: etykiety, stronicowanie i liczba zapytań."""

    def test_activity_reports_render_rows_and_keep_their_own_labels(self):
        """Raporty aktywności działają dla etapów z osobą i bez niej i mają własne nagłówki."""
        self.client.force_login(self.admin)
        for role, kind, name, title, prefix in (
            ("editor", "editing", "editor_activity", "Aktywność redaktorów", "editor"),
            ("proofreader_1", "first_proofreading", "proofreader_activity", "Aktywność korektorów", "proofreader"),
            ("verifier_1", "first_verification", "verifier_activity", "Aktywność weryfikatorów", "verifier"),
        ):
            text = Text.objects.create(title="Etap " + kind, length=1, anthology=self.book)
            assignment = A.objects.create(text=text, role=role, assigned_to=self.member)
            S.objects.create(text=text, stage_type=kind, assignment=assignment)
            other = Text.objects.create(title="Pusty " + kind, length=1, anthology=self.book)
            S.objects.create(text=other, stage_type=kind)
            response = self.client.get(reverse("core:" + name))
            self.assertEqual(response.status_code, 200)
            page = response.content.decode()
            self.assertIn(f'<h1 class="page-title">{title}</h1>', page)
            self.assertIn(f'id="{prefix}-activity-table"', page)
            self.assertIn(f'action="{reverse("core:" + name)}"', page)
        self.assertIn('Zakończony etap nie oznacza gotowej redakcji',
                      self.client.get(reverse('core:editor_activity')).content.decode())

    def test_report_projection_only_hydrates_one_page(self):
        from core.selectors.reports import reviewer_activity_context
        from core.pagination import paginate_items

        add_roles(self.member, "Recenzent")
        for number in range(32):
            review = Review.objects.create(title=f"Test {number:02}", anthology=self.book, length=1)
            ReviewAssignment.objects.create(
                review=review, user=self.member, position=1, opinion="yes"
            )
        rows = reviewer_activity_context(user=self.admin, params=QueryDict())["activity_rows"]
        self.assertIsNone(rows.queryset._result_cache)
        request = RequestFactory().get("/", {"sort": "title"})
        request.user = self.admin
        page = paginate_items(request, rows)
        self.assertEqual(page.paginator.count, 32)
        self.assertEqual(len(page.object_list), 25)
        self.assertEqual(page.object_list[0]["title"], "Test 00")

    def test_new_waiting_stage_gets_queue_date_unknown_old_stage_is_visible(self):
        text = Text.objects.create(title="Oczekuje", length=1, anthology=self.book)
        stage = S.objects.create(text=text, stage_type="ready_for_editing")
        self.assertEqual(stage.queued_at, timezone.localdate())
        S.objects.filter(pk=stage.pk).update(queued_at=None)
        rows = workflow_inactivity_context(user=self.admin, params=QueryDict("mode=waiting"))["rows"]
        self.assertIn(stage.pk, [row["stage"]["pk"] for row in rows])

    def test_text_projection_has_no_per_row_history_queries(self):
        """Lista tekstów: stała liczba zapytań niezależnie od liczby wierszy (teksty, etapy bieżącego
        cyklu, autorzy – bez przydziałów ról, potrzebnych tylko na stronie tekstu); wiersze bez zapytań."""
        from core.selectors.texts import _prepared_texts, _text_row

        for number in range(10):
            text = Text.objects.create(title=f'Query audit {number}', length=100, anthology=self.book)
            S.objects.create(text=text, stage_type='first_verification')
        query = Text.objects.filter(title__startswith='Query audit').order_by('pk')
        for limit in (1, 10):
            with self.subTest(limit=limit):
                with self.assertNumQueries(3):
                    prepared = list(_prepared_texts(query[:limit], False))
                with self.assertNumQueries(0):
                    for text in prepared:
                        row = _text_row(text, False)
                        self.assertEqual(row['current_stage_type'], 'first_verification')


class AssignmentConflictReportTests(TestCase):
    """Kontrola danych: wielokrotne przypisania tej samej osoby do tekstu oraz recenzje bez powiązanego tekstu."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('administrator', 'admin@example.test', 'test-only')
        cls.user = get_user_model().objects.create_user('anna', first_name='Anna', last_name='Testowa')
        cls.other = get_user_model().objects.create_user('inna', first_name='Anna', last_name='Testowa')
        cls.book = Anthology.objects.create(title='Antologia A')
        cls.text = Text.objects.create(title='Tekst A', anthology=cls.book, length=100)

    def assign(self, **kwargs):
        fields = {'text': self.text, 'assigned_to': self.user, 'role': 'editor', 'is_current': False}
        fields.update(kwargs)
        return A.objects.create(**fields)

    def rows(self, **kwargs):
        groups = assignment_conflicts(**kwargs)
        return assignment_conflict_details(groups, **{key: value for key, value in kwargs.items() if key != 'kind'})

    def page(self, **kwargs):
        self.client.force_login(self.admin)
        return self.client.get(reverse('core:data_integrity'), {'tab': 'assignments', **kwargs})

    def test_same_role_two_executions_are_listed_including_history(self):
        first = self.assign()
        second = self.assign(execution_number=2, is_current=True)
        row, = self.rows()
        self.assertEqual(row['assignment_count'], 2)
        self.assertEqual(row['reasons'], ['Ta sama rola w kilku przypisaniach'])
        self.assertEqual([a['id'] for a in row['assignments']], [first.pk, second.pk])
        self.assertEqual([a['execution_number'] for a in row['assignments']], [1, 2])
        self.assertEqual([a['status'] for a in row['assignments']], ['Historyczne wykonanie', 'Bieżące przypisanie'])

    def test_different_roles_are_listed_for_one_person(self):
        self.assign(is_current=True)
        self.assign(role='proofreader_1', is_current=True)
        row, = self.rows(kind='different_roles')
        self.assertEqual(row['reasons'], ['Różne role'])
        self.assertEqual(self.rows(kind='same_role'), [])

    def test_both_reasons_produce_one_pair_not_several_rows(self):
        self.assign()
        self.assign(execution_number=2)
        self.assign(role='proofreader_2')
        row, = self.rows()
        self.assertEqual(len(row['reasons']), 2)
        self.assertEqual(row['assignment_count'], 3)
        self.assertEqual(len(self.rows(kind='same_role')), 1)
        self.assertEqual(len(self.rows(kind='different_roles')), 1)

    def test_several_stages_of_one_assignment_are_not_duplicates(self):
        assignment = self.assign(is_current=True)
        for iteration in range(1, 4):
            S.objects.create(text=self.text, stage_type='editing', assignment=assignment, iteration=iteration)
        S.objects.create(text=self.text, stage_type='editor_control', assignment=assignment)
        self.assertEqual(self.rows(), [])

    def test_distinct_accounts_with_identical_names_are_not_merged(self):
        self.assign()
        self.assign(assigned_to=self.other, execution_number=2)
        self.assertEqual(self.rows(), [])

    def test_empty_assignments_and_separate_texts_are_not_combined(self):
        self.assign(assigned_to=None)
        self.assign(assigned_to=None, execution_number=2)
        self.assign(role='proofreader_1')
        other_text = Text.objects.create(title='Tekst B', anthology=self.book, length=100)
        self.assign(text=other_text)
        self.assertEqual(self.rows(), [])

    def test_cycles_can_be_scoped_without_hiding_earlier_executions(self):
        self.assign()
        Text.objects.filter(pk=self.text.pk).update(current_workflow_cycle=2)
        self.assign(workflow_cycle=2, is_current=True)
        row, = self.rows()
        self.assertEqual(row['assignments'][0]['status'], 'Poprzedni przebieg')
        self.assertEqual(self.rows(scope='current_cycle'), [])
        self.assign(workflow_cycle=2, execution_number=2)
        row, = self.rows(scope='current_cycle')
        self.assertEqual({a['workflow_cycle'] for a in row['assignments']}, {2})
        self.assertEqual(row['assignment_count'], 2)

    def test_canceled_repetition_remains_visible_and_labeled(self):
        self.assign()
        run = WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now())
        self.assign(execution_number=2, repetition=run)
        row, = self.rows()
        self.assertEqual(row['assignments'][1]['status'], 'Anulowane powtórzenie')

    def test_ready_texts_and_inactive_people_are_also_checked(self):
        self.assign()
        self.assign(role='proofreader_1')
        S.objects.create(text=self.text, stage_type='ready')
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(len(self.rows()), 1)

    def test_anthology_filter_and_exact_pair_details(self):
        self.assign()
        self.assign(role='proofreader_1')
        self.assign(assigned_to=self.other, role='verifier_1')
        other_book = Anthology.objects.create(title='Antologia B')
        other_text = Text.objects.create(title='Tekst B', anthology=other_book, length=100)
        self.assign(text=other_text)
        self.assign(text=other_text, execution_number=2)
        row, = self.rows(anthology_id=self.book.pk)
        self.assertEqual(row['user_id'], self.user.pk)
        self.assertEqual(row['assignment_count'], 2)
        self.assertEqual(len(self.rows()), 2)

    def test_query_count_does_not_grow_per_result_and_checks_are_read_only(self):
        for number in range(8):
            text = Text.objects.create(title=f'Tekst {number}', anthology=self.book, length=100)
            self.assign(text=text)
            self.assign(text=text, execution_number=2)
        before = list(A.objects.order_by('pk').values())
        with self.assertNumQueries(2):
            rows = self.rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual(list(A.objects.order_by('pk').values()), before)

    def test_page_shows_links_and_escapes_record_names(self):
        Text.objects.filter(pk=self.text.pk).update(title='<script>alert(1)</script>')
        self.assign()
        second = self.assign(execution_number=2)
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, reverse('admin:workflow_workflowroleassignment_change', args=[second.pk]))
        self.assertContains(response, 'wykonanie 2')
        self.assertNotContains(response, 'Możliwe duplikaty tekstów')
        self.assertNotContains(response, 'Sprawdź duplikaty')

    def test_removed_duplicate_scan_bookmark_opens_assignment_report(self):
        self.assign()
        self.assign(role='proofreader_1')
        response = self.page(tab='duplicates', run='1', anthology='all')
        self.assertEqual(response.context['tab'], 'assignments')
        self.assertEqual(response.context['assignment_page'].paginator.count, 1)
        import core.supervision
        self.assertFalse(hasattr(core.supervision, 'all_duplicates'))
        # The report removal does not remove intake validation used elsewhere.
        self.assertTrue(callable(core.supervision.duplicate_candidates))

    def test_invalid_filters_show_error_without_scanning(self):
        from unittest.mock import patch
        for params in ({'anthology': '9'*40}, {'anthology': 'bad'}, {'scope': 'bad'}, {'kind': 'bad'}):
            with self.subTest(params=params), patch('core.views.supervision.assignment_conflicts') as scan:
                response = self.page(**params)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['assignment_filters'].errors)
                self.assertIsNone(response.context['assignment_page'])
                scan.assert_not_called()

    def test_pagination_keeps_every_assignment_for_each_pair_and_filters(self):
        for number in range(26):
            text = Text.objects.create(title=f'Tekst {number:02}', anthology=self.book, length=100)
            self.assign(text=text)
            self.assign(text=text, execution_number=2)
        response = self.page(anthology=str(self.book.pk), kind='same_role', scope='current_cycle')
        page = response.context['assignment_page']
        self.assertEqual(page.paginator.count, 26)
        self.assertEqual(len(response.context['assignment_rows']), 25)
        self.assertIn('kind=same_role', page.next_url)
        self.assertIn('scope=current_cycle', page.next_url)
        last = self.page(page=2, anthology=str(self.book.pk), kind='same_role', scope='current_cycle')
        row, = last.context['assignment_rows']
        self.assertEqual(len(row['assignments']), 2)

    def test_empty_report_and_profile_name_fallback(self):
        self.assertContains(self.page(), 'Brak wielokrotnych przypisań')
        user = get_user_model().objects.create_user('empty-name')
        Person.objects.create(user=user, first_name='Jan', last_name='Profilowy', email='jan@example.test')
        self.assign(assigned_to=user)
        self.assign(assigned_to=user, execution_number=2)
        self.assertContains(self.page(), 'Jan Profilowy')

    def test_only_superuser_can_read_assignment_checks(self):
        url = reverse('core:data_integrity')
        self.assertEqual(self.client.get(url, {'tab': 'assignments'}).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url, {'tab': 'assignments'}).status_code, 403)
        self.assertEqual(self.client.post(url, {'tab': 'assignments'}).status_code, 405)

    def test_other_integrity_tabs_remain_available(self):
        self.client.force_login(self.admin)
        for tab in ('integrity', 'unlinked'):
            response = self.client.get(reverse('core:data_integrity'), {'tab': tab})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['tab'], tab)

    def test_unlinked_review_candidates_are_manual_and_superuser_only(self):
        from core.supervision import unlinked_review_candidates

        def review(**kw):
            data = dict(title=self.text.title, anthology=self.book, author_first_name="Jan",
                        author_last_name="Autor", email="autor@example.com", length=100,
                        genre="fantasy", status="accepted", author_notified_at=timezone.localdate())
            data.update(kw)
            return Review.objects.create(**data)

        candidate = review()
        review(title="Bez pasującego tekstu")
        review(publication_detached=True)
        self.assertEqual(list(unlinked_review_candidates()), [candidate])
        response = self.page(tab="unlinked")
        self.assertContains(response, "Recenzje do ręcznej kontroli: 1")
        candidate.refresh_from_db()
        self.assertIsNone(candidate.copied_text_id)
        self.assertFalse(candidate.publication_detached)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("core:data_integrity"), {"tab": "unlinked"}).status_code, 403
        )


class ReportPageTests(TestCase):
    """Strony raportów dla koordynatora i superusera: tryby, filtry, sortowanie i ukrywanie danych autorów."""

    @classmethod
    def setUpTestData(cls):
        from authors.models import Author
        from fantazmaty.testing import create_member, create_superuser

        cls.today = timezone.localdate()
        cls.admin = create_superuser()
        cls.coordinator = create_member('koordynator', 'Koordynator')
        cls.editor = create_member('redaktor', 'Redaktor')
        cls.other_editor = create_member('inny', 'Redaktor')
        cls.book = Anthology.objects.create(title='Antologia A')
        cls.other_book = Anthology.objects.create(title='Antologia B')
        cls.author = Author.objects.create(first_name='Ukryty', last_name='Autorski', email='autor@example.test')
        cls.stalled = Text.objects.create(title='Zastój redakcji', length=100, anthology=cls.book)
        cls.stalled.authors.add(cls.author)
        editor = A.objects.create(text=cls.stalled, role='editor', assigned_to=cls.editor)
        S.objects.create(text=cls.stalled, stage_type='editing', assignment=editor,
                         started_at=cls.today - timedelta(days=40))
        cls.waiting = Text.objects.create(title='Czeka na korektę', length=100, anthology=cls.other_book)
        S.objects.create(text=cls.waiting, stage_type='first_proofreading', queued_at=cls.today - timedelta(days=10))
        history = [(cls.stalled, editor, 50)]
        for user, number in ((cls.other_editor, 1), (cls.editor, 2)):
            history.append((cls.waiting, A.objects.create(text=cls.waiting, role='editor', assigned_to=user,
                                                          execution_number=number, is_current=False), 4 - number))
        for text, assignment, days in history:
            S.objects.create(text=text, stage_type='author_editing', assignment=assignment,
                             iteration=assignment.execution_number, execution_number=assignment.execution_number,
                             started_at=cls.today - timedelta(days=days + 1), ended_at=cls.today - timedelta(days=days),
                             is_completed=True, is_current=False)

    def get(self, name, user=None, **params):
        self.client.force_login(user or self.admin)
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, 200)
        return response

    def inactivity_titles(self, user=None, **params):
        return [row['text']['title'] for row in self.get('core:workflow_inactivity', user, **params).context['rows']]

    def test_inactivity_modes_filters_and_author_column(self):
        self.assertEqual(self.inactivity_titles(), ['Zastój redakcji', 'Czeka na korektę'])
        self.assertEqual(self.inactivity_titles(mode='active'), ['Zastój redakcji'])
        self.assertEqual(self.inactivity_titles(mode='waiting'), ['Czeka na korektę'])
        self.assertEqual(self.inactivity_titles(mode='nieznany'), ['Zastój redakcji', 'Czeka na korektę'])
        self.assertEqual(self.inactivity_titles(stage='first_proofreading'), ['Czeka na korektę'])
        self.assertEqual(self.inactivity_titles(q='zastoj'), ['Zastój redakcji'])
        self.assertEqual(self.inactivity_titles(q='Autorski'), ['Zastój redakcji'])
        self.assertContains(self.get('core:workflow_inactivity'), 'Autorski')
        page = self.get('core:workflow_inactivity', self.coordinator)
        self.assertNotContains(page, 'Autorski')
        self.assertEqual([r['text']['title'] for r in page.context['rows']], ['Zastój redakcji', 'Czeka na korektę'])
        self.assertEqual(self.inactivity_titles(self.coordinator, q='Autorski'), [])
        reviews = self.get('core:workflow_inactivity', mode='reviews')
        self.assertFalse(reviews.context['show_workflow_rows'])
        self.assertTrue(reviews.context['show_review_section'])
        self.assertFalse(self.get('core:workflow_inactivity', mode='active').context['show_review_section'])
        with self.assertRaises(ValueError):
            workflow_inactivity_context(user=self.admin, params=QueryDict(), active_days=0)

    def test_overdue_review_assignments_are_listed(self):
        review = Review.objects.create(title='Długa lektura', anthology=self.book, length=1, status='in_review')
        assignment = ReviewAssignment.objects.create(review=review, user=self.editor, position=1, opinion='reading')
        ReviewAssignment.objects.filter(pk=assignment.pk).update(assigned_at=timezone.now() - timedelta(days=30))
        rows = self.get('core:workflow_inactivity').context['overdue_reviews']
        self.assertEqual([(row['review'].title, row['days']) for row in rows], [('Długa lektura', 30)])

    def test_editor_activity_filters_dates_people_and_sorting(self):
        def rows(user=None, **params):
            page = self.get('core:editor_activity', user, **params)
            return page, [(r['title'], r['person_name'], r['role']) for r in page.context['activity_rows']]

        page, all_rows = rows()
        self.assertEqual(all_rows[0], ('Czeka na korektę', 'redaktor Testowy', 'Redakcja (wyk. 2)'))
        self.assertIn(('Zastój redakcji', 'redaktor Testowy', 'Redaktor'), all_rows)
        self.assertEqual({p['pk'] for p in page.context['editors']},
                         {self.editor.person_profile.pk, self.other_editor.person_profile.pk})
        recent_from = (self.today - timedelta(days=10)).isoformat()
        self.assertEqual(len(rows(date_from=recent_from)[1]), 2)
        self.assertEqual(rows(date_to=(self.today - timedelta(days=10)).isoformat())[1],
                         [('Zastój redakcji', 'redaktor Testowy', 'Redaktor')])
        page, invalid = rows(date_from=recent_from, date_to=(self.today - timedelta(days=20)).isoformat())
        self.assertEqual(invalid, [])
        self.assertTrue(page.context['filter_errors'])
        page, by_person = rows(person=self.other_editor.person_profile.pk)
        self.assertEqual(by_person, [('Czeka na korektę', 'inny Testowy', 'Redaktor')])
        # Each filter lists only options that still give results with the other filters.
        self.assertEqual([a['pk'] for a in page.context['anthologies']], [self.other_book.pk])
        page, by_book = rows(anthology=self.book.pk)
        self.assertEqual({row[0] for row in by_book}, {'Zastój redakcji'})
        self.assertEqual([p['pk'] for p in page.context['editors']], [self.editor.person_profile.pk])
        self.assertEqual(rows(q='inny')[1], [('Czeka na korektę', 'inny Testowy', 'Redaktor')])
        self.assertEqual([row[0] for row in rows(sort='title')[1]][0], 'Czeka na korektę')
        self.assertEqual([row[0] for row in rows(sort='-title')[1]][0], 'Zastój redakcji')
        self.assertEqual([row[0] for row in rows(sort='-authors')[1]][0], 'Zastój redakcji')
        self.assertEqual(rows(self.coordinator, q='Autorski')[1], [])
        self.assertEqual({row[0] for row in rows(q='Autorski')[1]}, {'Zastój redakcji'})

    def test_last_activity_lists_people_without_recent_personal_work(self):
        from core.models import WorkflowEvent

        WorkflowEvent.objects.create(title='T', actor=self.editor, actor_name='redaktor', previous_status='',
                                     next_status='', personal_work=True,
                                     personal_work_description='Zakończono redakcję')

        def names(**params):
            return {str(person) for person in self.get('core:last_activity', self.coordinator, **params).context['people']}

        idle = names()
        self.assertNotIn('redaktor Testowy', idle)
        self.assertIn('inny Testowy', idle)
        self.assertEqual(names(days='nie-liczba'), idle)
        self.assertIn('redaktor Testowy', names(days='0'))
        self.assertIn('redaktor Testowy', names(mode='login'))
        self.assertEqual(self.get('core:last_activity', self.coordinator, mode='x').context['mode'], 'work')

    def test_last_activity_filters_by_role_and_marks_people_on_leave(self):
        from people.models import Role, Vacation

        redaktor = Role.objects.get(name='Redaktor')
        Vacation.objects.create(person=self.other_editor.person_profile, start_date=self.today, until_revoked=True)
        Vacation.objects.create(person=self.editor.person_profile, start_date=self.today - timedelta(days=9),
                                end_date=timezone.now() - timedelta(days=1))
        page = self.get('core:last_activity', self.coordinator, role=redaktor.pk, days=0)
        self.assertEqual({str(person) for person in page.context['people']}, {'redaktor Testowy', 'inny Testowy'})
        self.assertEqual({person.pk: person.on_leave for person in page.context['people']},
                         {self.other_editor.person_profile.pk: True, self.editor.person_profile.pk: False})
        self.assertEqual(page.context['role'], str(redaktor.pk))
        self.assertContains(page, 'inny Testowy</a> <span class="badge is-warning">Na urlopie</span>', html=False)
        self.assertContains(page, 'Na urlopie', count=1)
        everyone = self.get('core:last_activity', self.coordinator, role='nie-rola', days=0)
        self.assertEqual(everyone.context['role'], '')
        self.assertIn('koordynator Testowy', {str(person) for person in everyone.context['people']})

    def test_reviewer_activity_archive_scope(self):
        from core.selectors.reports import reviewer_activity_context

        for title, status in (('Otwarte', 'in_review'), ('Przyjęte', 'accepted')):
            review = Review.objects.create(title=title, anthology=self.book, length=1, status=status)
            ReviewAssignment.objects.create(review=review, user=self.editor, position=1, opinion='yes')

        def titles(**params):
            context = reviewer_activity_context(user=self.admin, params=QueryDict(mutable=True) | params)
            return context['selected_archive'], sorted(row['title'] for row in context['activity_rows'])

        self.assertEqual(titles(), ('current', ['Otwarte']))
        self.assertEqual(titles(archive='archived'), ('archived', ['Przyjęte']))
        self.assertEqual(titles(archive='all'), ('all', ['Otwarte', 'Przyjęte']))
        self.assertEqual(titles(archive='inne'), ('current', ['Otwarte']))
        self.assertEqual(titles(archive='all', status='accepted'), ('all', ['Przyjęte']))
        self.assertEqual(titles(archive='all', date_from=(self.today + timedelta(days=1)).isoformat()), ('all', []))
        self.assertEqual(titles(archive='all', date_to=self.today.isoformat()), ('all', ['Otwarte', 'Przyjęte']))
        self.assertEqual(titles(date_from='wczoraj'), ('current', []))
        self.assertContains(self.get('core:reviewer_activity', archive='all'), 'Przyjęte')
        with self.assertRaises(ValueError):
            from core.selectors.reports import workflow_activity_context
            workflow_activity_context(user=self.admin, params=QueryDict(), stage_roles={'fourth_verification': 'verifier_4'},
                                      people_role_name='Redaktor', people_context_name='editors')
