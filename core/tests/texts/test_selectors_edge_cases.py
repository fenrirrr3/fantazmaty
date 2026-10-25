"""Selektory list i strony tekstu: filtr „bez etapu”, wyszukiwanie bez danych autorów,
widok „Moje teksty” z nieznanym parametrem, tabela zbiorcza z rezerwacją oraz szczegóły
etapów widoczne dla koordynatora i superusera."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, ReviewAssignment, Text
from workflow.archive_executions import SOURCE
from workflow.import_context import importing_completed
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S

from .support import source_review


class ListSelectorTests(TestCase):
    """Listy tekstów i prac: filtry i wyszukiwanie zależne od uprawnień."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser('listy-admin')
        cls.member = create_member('listy-redaktor', 'Redaktor')
        cls.coordinator = create_member('listy-koordynator', 'Koordynator redakcji')
        cls.book = Anthology.objects.create(title='Antologia')
        cls.author = Author.objects.create(first_name='Ewa', last_name='Wrzosowska', email='ewa@example.test')
        cls.waiting = Text.objects.create(title='Mgła', anthology=cls.book, length=100)
        cls.waiting.authors.add(cls.author)
        S.objects.create(text=cls.waiting, stage_type='ready_for_editing')
        cls.fresh = Text.objects.create(title='Mgławica', anthology=cls.book, length=100)

    def titles(self, user, route, params, key='texts'):
        self.client.force_login(user)
        response = self.client.get(reverse(route), params)
        self.assertEqual(response.status_code, 200)
        return response, response.context[key]

    def test_status_filter_none_lists_texts_without_stage(self):
        _, texts = self.titles(self.member, 'core:text_list', {'status': 'none'})
        self.assertEqual([row['title'] for row in texts], ['Mgławica'])

    def test_author_search_requires_author_data_permission(self):
        """Członek zespołu i koordynator szukają tylko po tytule; superuser także po autorze."""
        cases = (
            (self.member, 'core:text_list', 'texts', 'Wrzosowska', []),
            (self.member, 'core:text_list', 'texts', 'Mgła', ['Mgła', 'Mgławica']),
            (self.coordinator, 'core:workflow_list', 'stages', 'Wrzosowska', []),
            (self.coordinator, 'core:workflow_list', 'stages', 'Mgławica', ['Mgławica']),
            (self.admin, 'core:available_texts', 'available_stages', 'Wrzosowska', ['Mgła']),
        )
        for user, route, key, query, expected in cases:
            with self.subTest(user=user.username, route=route, query=query):
                _, rows = self.titles(user, route, {'q': query}, key)
                titles = [row['title'] if 'title' in row else row['text']['title'] for row in rows]
                self.assertEqual(sorted(titles), expected)

    def test_unknown_my_texts_view_falls_back_to_active(self):
        response, _ = self.titles(self.member, 'core:my_texts', {'view': 'kosz'})
        self.assertEqual(response.context['selected_view'], 'active')

    def test_workflow_table_marks_future_booking_as_reserved(self):
        editor = A.objects.create(text=self.fresh, role='editor', assigned_to=self.member)
        S.objects.create(text=self.fresh, stage_type='editing', assignment=editor,
                         started_at=self.today + timedelta(days=3))
        _, rows = self.titles(self.coordinator, 'core:workflow_list', {'q': 'Mgławica'}, 'stages')
        row, = rows
        editor_cell = next(cell for cell in row['role_cells'] if cell['role'] == 'editor')
        entry, = editor_cell['entries']
        self.assertEqual((entry['state'], entry['tone']), ('Zarezerwowano', 'pending'))
        self.assertEqual(entry['user']['pk'], self.member.pk)


class DetailSelectorTests(TestCase):
    """Strona tekstu: historia dla koordynatora, kontrola redaktora, stylowanie i opinie recenzji źródłowej."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser('detal-admin')
        cls.editor = create_member('detal-redaktor', 'Redaktor')
        cls.coordinator = create_member('detal-koordynator', 'Koordynator redakcji')
        cls.reviewer = create_member('detal-recenzent', 'Recenzent')
        cls.book = Anthology.objects.create(title='Antologia')

    def setUp(self):
        self.text = Text.objects.create(title='Szczegóły', anthology=self.book, length=100)

    def detail(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context

    def done(self, kind, **extra):
        return S.objects.create(text=self.text, stage_type=kind, started_at=self.today,
                                ended_at=self.today, is_completed=True, **extra)

    def test_editor_control_and_styling_rows_follow_their_owners(self):
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        control = S.objects.create(text=self.text, stage_type='editor_control', assignment=editor)
        styling_role = A.objects.create(text=self.text, role='styling', assigned_to=self.admin)
        styling = S.objects.create(text=self.text, stage_type='styling', assignment=styling_role)
        cases = (
            (self.editor, {control.pk: True, styling.pk: False}),
            (self.coordinator, {control.pk: False, styling.pk: False}),
            (self.admin, {control.pk: False, styling.pk: True}),
        )
        for user, expected in cases:
            with self.subTest(user=user.username):
                rows = {row['pk']: row['can_start'] for row in self.detail(user)['stages']}
                self.assertEqual({pk: rows[pk] for pk in expected}, expected)

    def test_source_review_opinions_are_listed(self):
        review = source_review(self.text)
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion='yes')
        ReviewAssignment.objects.create(review=review, user=None, position=2)
        opinions = self.detail(self.admin)['source_review_opinions']
        self.assertEqual([(item['position'], item['reviewer_name']) for item in opinions],
                         [(1, 'detal-recenzent Testowy'), (2, 'Usunięte konto')])
        self.assertEqual(opinions[0]['opinion_value'], 'yes')

    def test_finished_archive_text_lists_performers_of_imported_work(self):
        """Gotowy tekst z tabeli archiwalnej: zespół to wykonawcy zaimportowanej pracy, w kolejności ról."""
        Text.objects.filter(pk=self.text.pk).update(import_source=SOURCE, import_source_row=2)
        token = importing_completed.set(True)
        try:
            for role, kind, user in (('verifier_1', 'first_verification', self.reviewer),
                                     ('editor', 'editing', self.editor)):
                assignment = A.objects.create(text=self.text, role=role, assigned_to=user, is_current=False)
                S.objects.create(text=self.text, stage_type=kind, assignment=assignment, is_completed=True,
                                 imported_completed=True, is_current=False, is_released=False)
        finally:
            importing_completed.reset(token)
        self.done('ready')
        team = self.detail(self.admin)['team_members']
        self.assertEqual([(member['role'], member['user']['pk']) for member in team],
                         [('editor', self.editor.pk), ('verifier_1', self.reviewer.pk)])
        self.assertTrue(all(member['archive_execution'] for member in team))
