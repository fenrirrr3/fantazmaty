"""Regressions reproduced by the archive audit on 3 October 2026."""
from datetime import timedelta

from importlib import import_module
from pathlib import Path
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command, get_commands, CommandError
from django.db.migrations.state import ProjectState
from django.db import connection
from django.http import QueryDict
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.templatetags.static import static
from django.urls import reverse

from core.test_status_assignment_regression import StatusAssignmentFixtures
from people.models import Person, Role, Vacation
from texts.models import Review, Text
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S


class AuditOctoberRegressions(StatusAssignmentFixtures):
    def role(self, user, name):
        user.person_profile.roles.add(Role.objects.get_or_create(name=name)[0])

    def csrf_client(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        client.get(reverse('password_change'))
        return client, client.cookies[settings.CSRF_COOKIE_NAME].value

    def test_hidden_review_conflict_respects_visibility_with_valid_csrf(self):
        self.role(self.other, 'Koordynator')
        review = Review.objects.create(
            title='Private hidden title', content_warnings='Private warnings',
            length=100, anthology=self.book, is_hidden=True,
        )
        for user, endpoint in ((self.member, 'assign_reviewer'), (self.other, 'update_review_status')):
            client, csrf = self.csrf_client(user)
            for version in (None, 'invalid'):
                with self.subTest(user=user.pk, version=version):
                    data = {'status': 'accepted'}
                    if version is not None:
                        data['_edit_version'] = version
                    response = client.post(reverse('core:' + endpoint, args=[review.pk]), data,
                                           HTTP_X_CSRFTOKEN=csrf)
                    self.assertIn(response.status_code, (403, 404))
                    self.assertNotIn(b'Private hidden title', response.content)
                    self.assertNotIn(b'Private warnings', response.content)
        review.refresh_from_db()
        self.assertEqual(review.status, Review.Status.NEW)
        self.assertFalse(review.assignments.exists())

    def test_superuser_retains_conflict_for_hidden_review(self):
        review = Review.objects.create(title='Hidden for ordinary users', length=100,
                                       anthology=self.book, is_hidden=True)
        client, csrf = self.csrf_client(self.admin)
        response = client.post(reverse('core:update_review_status', args=[review.pk]),
                               {'_edit_version': 'invalid'}, HTTP_X_CSRFTOKEN=csrf)
        self.assertContains(response, review.title, status_code=409)

    def test_visible_review_retains_conflict_for_coordinator(self):
        self.role(self.member, 'Koordynator')
        review = Review.objects.create(title='Visible review', length=100, anthology=self.book)
        self.client.force_login(self.member)
        response = self.client.post(reverse('core:update_review_status', args=[review.pk]),
                                    {'_edit_version': 'invalid'})
        self.assertContains(response, review.title, status_code=409)

    def test_archive_conflict_does_not_bypass_admin_only_withdrawn_reviews(self):
        # The archive is visible to the whole team; withdrawn submissions stay in the admin panel.
        review = Review.objects.create(title='Private withdrawn entry', length=100,
                                       anthology=self.book, status='withdrawn')
        self.client.force_login(self.member)
        response = self.client.post(reverse('core:assign_reviewer', args=[review.pk]),
                                    {'_edit_version': 'invalid'})
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn(review.title.encode(), response.content)

    def test_retired_merge_migration_preserves_all_performers(self):
        migration = import_module(
            'workflow.migrations.0013_merge_duplicate_role_assignments'
        ).Migration('0013_merge_duplicate_role_assignments', 'workflow')
        first = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        second = A.objects.create(text=self.text, role='editor', assigned_to=self.other,
                                  execution_number=2, is_current=False)
        third = A.objects.create(text=self.text, role='editor', assigned_to=self.member,
                                 execution_number=3, is_current=False)
        for assignment in (first, second, third):
            S.objects.create(text=self.text, stage_type='editing', assignment=assignment,
                             started_at=self.today, ended_at=self.today,
                             execution_number=assignment.execution_number,
                             iteration=assignment.execution_number,
                             is_completed=True, is_current=False)
        before_assignments = list(A.objects.filter(text=self.text).order_by('pk').values())
        before_stages = list(S.objects.filter(text=self.text).order_by('pk').values())
        migration.apply(ProjectState.from_apps(apps), None)
        self.assertEqual(list(A.objects.filter(text=self.text).order_by('pk').values()), before_assignments)
        self.assertEqual(list(S.objects.filter(text=self.text).order_by('pk').values()), before_stages)

    def test_bulk_merge_command_and_entry_point_are_removed(self):
        self.assertFalse((Path(__file__).resolve().parents[1] / 'scal_przypisania.py').exists())
        self.assertNotIn('merge_workflow_assignments', get_commands())
        with self.assertRaisesMessage(CommandError, 'Unknown command'):
            call_command('merge_workflow_assignments')
        self.assertFalse(hasattr(import_module('workflow.assignment_merge'), 'merge_duplicates'))

    def test_unlinked_person_leave_does_not_empty_candidate_lists(self):
        from workflow.repetitions import eligible_repeat_users
        from workflow.handoffs import eligible_handoff_users

        self.role(self.member, 'Redaktor')
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.other)
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=assignment)
        before = list(eligible_repeat_users(self.text, 'editor'))
        person = Person.objects.create(first_name='Leave', last_name='No account', email='leave@example.test')
        Vacation.objects.create(person=person, start_date=self.today, until_revoked=True)
        self.assertEqual(list(eligible_repeat_users(self.text, 'editor')), before)
        self.assertIn(self.member, before)
        self.assertIn(self.member, eligible_handoff_users(stage))
        Vacation.objects.create(person=self.member.person_profile, start_date=self.today, until_revoked=True)
        self.assertNotIn(self.member, eligible_repeat_users(self.text, 'editor'))
        self.assertNotIn(self.member, eligible_handoff_users(stage))

    def test_legacy_group_does_not_offer_candidate_rejected_by_write(self):
        from core.services.texts import _require_eligible_assignee
        from workflow.availability import eligible_role_users

        group = Group.objects.get_or_create(name='Redaktor')[0]
        type(self.member).groups.through.objects.create(user_id=self.member.pk, group_id=group.pk)
        self.assertNotIn(self.member, eligible_role_users('editor'))
        with self.assertRaises(ValidationError):
            _require_eligible_assignee(self.member, 'editor')
        self.role(self.member, 'Redaktor')
        self.assertIn(self.member, eligible_role_users('editor'))
        _require_eligible_assignee(self.member, 'editor')

    def test_candidate_roles_preserve_special_proofreading_policy(self):
        from workflow.availability import eligible_role_users

        self.role(self.member, 'Koordynator')
        self.assertIn(self.member, eligible_role_users('editor'))
        self.assertNotIn(self.member, eligible_role_users(A.Role.PROOFREADER_2))
        self.assertNotIn(self.member, eligible_role_users(A.Role.STYLING))
        self.role(self.member, 'Koordynator korekty')
        self.assertIn(self.member, eligible_role_users(A.Role.PROOFREADER_2))
        self.assertIn(self.member, eligible_role_users(A.Role.PROOFREADER_4))
        self.assertIn(self.admin, eligible_role_users(A.Role.STYLING))

    def test_text_projection_has_no_per_row_history_queries(self):
        from core.selectors.texts import _prepared_texts, _text_row

        for number in range(10):
            text = Text.objects.create(title=f'Query audit {number}', length=100, anthology=self.book)
            S.objects.create(text=text, stage_type='first_verification')
        query = Text.objects.filter(title__startswith='Query audit').order_by('pk')
        counts = []
        for limit in (1, 10):
            with CaptureQueriesContext(connection) as queries:
                prepared = list(_prepared_texts(query[:limit], False))
            counts.append(len(queries))
            with self.assertNumQueries(0):
                for text in prepared:
                    row = _text_row(text, False)
                    self.assertEqual(row['current_stage_type'], 'first_verification')
        self.assertEqual(counts[0], counts[1])

    def test_prefetched_history_and_inactivity_ignore_obsolete_placeholder(self):
        from core.selectors.texts import _prepared_texts, _text_row
        from core.selectors.reports import workflow_inactivity_context

        S.objects.create(text=self.text, stage_type='first_verification',
                         started_at=self.today-timedelta(days=60), ended_at=self.today-timedelta(days=59),
                         is_completed=True, is_current=False)
        pending = S.objects.create(text=self.text, stage_type='first_verification', iteration=2,
                                   queued_at=self.today-timedelta(days=40))
        prepared = list(_prepared_texts(Text.objects.filter(pk=self.text.pk), False))[0]
        with self.assertNumQueries(0):
            self.assertEqual(_text_row(prepared, False)['current_stages'], [])
        report = workflow_inactivity_context(user=self.admin, params=QueryDict(''), today=self.today)
        self.assertNotIn(pending.pk, [row['stage']['pk'] for row in report['rows']])

    def test_password_change_uses_cms_layout_and_preserves_validation(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('password_change'))
        self.assertTemplateUsed(response, 'core/base.html')
        self.assertContains(response, static('core/components.css'))
        response = self.client.post(reverse('password_change'),
                                    {'old_password': 'incorrect', 'new_password1': 'New-password-927!',
                                     'new_password2': 'New-password-927!'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password('test'))

    def test_password_change_success_keeps_session_and_cms_layout(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse('password_change'),
                                    {'old_password': 'test', 'new_password1': 'New-password-927!',
                                     'new_password2': 'New-password-927!'}, follow=True)
        self.assertTemplateUsed(response, 'registration/password_change_done.html')
        self.assertTemplateUsed(response, 'core/base.html')
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password('New-password-927!'))
