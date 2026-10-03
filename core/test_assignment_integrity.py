from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.assignment_integrity import assignment_conflicts, assignment_conflict_details
from people.models import Person
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S, WorkflowRepetition


class AssignmentIntegrityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('administrator', 'admin@example.test', 'test-only')
        cls.user = get_user_model().objects.create_user('anna', first_name='Anna', last_name='Testowa')
        cls.other = get_user_model().objects.create_user('inna', first_name='Anna', last_name='Testowa')
        cls.book = Anthology.objects.create(title='Antologia A')
        cls.text = Text.objects.create(title='Tekst A', anthology=cls.book, length=100)

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        override.enable()
        self.addCleanup(override.disable)

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
