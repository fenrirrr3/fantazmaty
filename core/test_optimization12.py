from datetime import timedelta
from random import Random
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin
from core.permissions import read_access_scope, has_role, is_coordinator, is_team_member
from core.selectors.texts import (_prepared_texts, _user_texts, _own_work, STAGE_ROLE_MAP,
    my_texts_context, available_stages_for_user, user_workflow_summary)
from core.pagination import paginate_items
from texts.models import Text, Review, ReviewAssignment
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.availability import claim_reason, claim_access
from people.models import Role, Person
from authors.models import Author


class Optimization12Tests(CoreTestDataMixin, TestCase):
    def text(self, number, *, kind='ready_for_editing', user=None):
        text = Text.objects.create(title=f'Tekst {number:04}', length=100, anthology=self.anthology)
        S.objects.create(text=text, stage_type=kind, started_at=timezone.localdate() if user else None)
        if user:
            A.objects.create(text=text, role=STAGE_ROLE_MAP[kind], assigned_to=user)
        return text

    def test_database_predicates_match_workflow_rules(self):
        rng = Random(1200)
        today = timezone.localdate()
        kinds = [kind for kind, _ in S.StageType.choices]
        users = [self.editor, self.coordinator, self.superuser]
        for number in range(140):
            text = Text.objects.create(title=f'Wariant {number:03}', length=100, current_workflow_cycle=2)
            stages = []
            for cycle in (1, 2):
                for kind in rng.sample(kinds, rng.randint(1, min(8, len(kinds)))):
                    state = rng.randrange(5)
                    started = None if state == 0 else today + timedelta(days=1 if state == 4 else -2)
                    ended = today if state in (2, 3) else None
                    stages.append(S(text=text, workflow_cycle=cycle, stage_type=kind,
                                    started_at=started, ended_at=ended, is_completed=state == 3))
                for role in rng.sample(list(A.Role.values), rng.randint(0, 5)):
                    # Keep database constraint on the two primary verifiers valid.
                    if role == 'verifier_2':
                        continue
                    A.objects.create(text=text, workflow_cycle=cycle, role=role,
                                     assigned_to=rng.choice(users + [None]))
            S.objects.bulk_create(stages)
        for user in users:
            access = claim_access(user)
            expected_available = []
            for text in _prepared_texts(Text.objects.order_by('anthology__title', 'title', 'pk'), False):
                for stage in text.selector_stages:
                    if not claim_reason(stage, user, text.selector_stages, text.selector_assignments, access=access):
                        expected_available.append(stage.pk)
            self.assertEqual([row['pk'] for row in available_stages_for_user(user=user)], expected_available)
            expected = {view: [] for view in ('active', 'waiting', 'completed', 'all')}
            active_ids, reserved_ids = [], []
            for text in _user_texts(user, False):
                assignments, active, reserved, waiting = _own_work(text, user, today)
                roles = {a.role for a in assignments}
                own = [s for s in text.selector_stages if STAGE_ROLE_MAP.get(s.stage_type) in roles
                       and s.stage_type not in ('author_editing', 'ready_for_editing')]
                completed = bool(own) and all(s.is_completed for s in own) and not active and not reserved and not waiting
                for view, match in [('all', True), ('active', bool(active)), ('waiting', bool(waiting or reserved)), ('completed', completed)]:
                    if match: expected[view].append(text.pk)
                active_ids.extend(s.pk for s in active)
                reserved_ids.extend(a.pk for a in reserved)
            for view in expected:
                actual = my_texts_context(user=user, selected_view=view)['texts']
                self.assertEqual([row['pk'] for row in actual], expected[view], (user.pk, view))
            summary = user_workflow_summary(user)
            self.assertEqual({r['pk'] for r in summary['active_stages']}, set(active_ids))
            self.assertEqual([r['pk'] for r in summary['reserved_assignments']], reserved_ids)

    def test_available_and_my_lists_only_project_current_page(self):
        for number in range(60):
            self.text(number)
            self.text(number + 100, kind='editing', user=self.editor)
        request = RequestFactory().get('/', {'page': 2, 'page_size': 25})
        from core.selectors.texts import _stage_data, _text_row
        with read_access_scope(), patch('core.selectors.texts._stage_data', wraps=_stage_data) as project:
            with CaptureQueriesContext(connection) as queries:
                page = paginate_items(request, available_stages_for_user(user=self.editor))
                rows = list(page)
            self.assertEqual(page.paginator.count, 60)
            self.assertEqual(len(rows), 25)
            self.assertEqual(project.call_count, 25)
            self.assertTrue(any('LIMIT 25 OFFSET 25' in q['sql'] for q in queries))
        with patch('core.selectors.texts._text_row', wraps=_text_row) as project:
            page = paginate_items(request, my_texts_context(user=self.editor)['texts'])
            self.assertEqual(len(list(page)), 25)
            self.assertEqual(page.paginator.count, 60)
            self.assertEqual(project.call_count, 25)

    def test_permission_cache_is_scoped_and_does_not_survive_reads(self):
        with read_access_scope():
            self.assertTrue(has_role(self.editor, 'Redaktor'))
            with self.assertNumQueries(0):
                for _ in range(10):
                    self.assertTrue(is_team_member(self.editor))
                    self.assertFalse(is_coordinator(self.editor))
                    self.assertTrue(has_role(self.editor, 'Redaktor'))
        self.editor_person.roles.clear()
        self.assertFalse(has_role(self.editor, 'Redaktor'))
        with read_access_scope():
            self.assertFalse(has_role(self.editor, 'Redaktor'))
        self.editor_person.is_active=False
        self.editor_person.save(update_fields=['is_active'])
        self.assertFalse(is_team_member(self.editor))

    def test_dashboard_only_loads_six_and_fetches_own_next_page(self):
        for number in range(14):
            self.text(number, kind='editing', user=self.editor)
        other=self.text(99, kind='editing', user=self.coordinator)
        self.client.force_login(self.editor)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.context['active_stage_count'], 14)
        self.assertEqual(len(response.context['active_stages']), 6)
        self.assertNotContains(response, 'Tekst 0006')
        response = self.client.get(reverse('core:dashboard_tasks'), {'kind':'active', 'page':2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['html'].count('dashboard-list-item'), 6)
        self.assertNotIn(other.title, response.json()['html'])
        self.assertIn('page=3', response.json()['next_url'])
        self.assertEqual(self.client.get(reverse('core:dashboard_tasks'), {'kind':'bad'}).status_code, 400)
        self.editor_person.is_active=False; self.editor_person.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(reverse('core:dashboard_tasks')).status_code, 403)

    def test_reviewer_dashboard_skips_workflow_and_loads_reviews_lazily(self):
        for number in range(8):
            review=Review.objects.create(title=f'Recenzja {number}', anthology=self.anthology, status='in_review', length=100)
            ReviewAssignment.objects.create(review=review, user=self.reviewer, opinion='reading', position=1)
        self.client.force_login(self.reviewer)
        with patch('core.views.dashboard.user_workflow_summary', side_effect=AssertionError('unneeded workflow')):
            response=self.client.get(reverse('core:home'))
        self.assertEqual(response.context['active_stage_count'],8)
        self.assertEqual(len(response.context['review_reading']),6)
        response=self.client.get(reverse('core:dashboard_tasks'),{'kind':'active','page':2})
        self.assertEqual(response.json()['html'].count('dashboard-list-item'),2)
        self.assertIsNone(response.json()['next_url'])

    def test_search_contact_checks_are_bounded_and_private(self):
        from core.views.search import _search_people
        person=Person.objects.create(first_name='Wynik',last_name='Kontaktowy',email=self.author.email,is_active=True)
        for number in range(40):
            Author.objects.create(first_name='Inny',last_name=str(number),email=f'other{number}@example.com')
        with read_access_scope(), CaptureQueriesContext(connection) as queries:
            results=_search_people('Kontaktowy',self.editor)
        self.assertEqual(results[0]['email'],'')
        email_queries=[q['sql'] for q in queries if 'AS "contact_email"' in q['sql'] or ('LOWER(' in q['sql'] and 'authors_author' in q['sql'])]
        self.assertEqual(len(email_queries),2)
        for sql in email_queries:
            self.assertIn(' IN (',sql)
            self.assertIn(self.author.email,sql)
        text=self.text(1,kind='editing',user=self.editor);text.authors.add(self.author)
        with read_access_scope():
            self.assertEqual(_search_people('Kontaktowy',self.editor)[0]['email'],self.author.email)
