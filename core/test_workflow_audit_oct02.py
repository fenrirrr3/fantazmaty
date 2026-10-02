"""Regression checks for the workflow entry points reviewed on 2026-10-02."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from people.models import Vacation

from core.workflow_tokens import make_token
from core.edit_versions import version_of
from core.services.texts import start_assigned_stage
from workflow.admin_add_stage import add_missing_stage
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.services import start_first_verification
from workflow.tests import WorkflowTestDataMixin


class VerificationStartAuditTests(WorkflowTestDataMixin, TestCase):
    def pending_first(self, editorial_type):
        self.initial_stage.is_current = False
        self.initial_stage.save(update_fields=['is_current'])
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        editorial = S.objects.create(text=self.text, stage_type=editorial_type, assignment=editor)
        verifier = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.verifier_1)
        first = S.objects.create(text=self.text, stage_type='first_verification', assignment=verifier)
        return editorial, first

    def assert_start_blocked(self, kind):
        editorial, first = self.pending_first(kind)
        for start in (
            lambda: start_first_verification(self.text, self.verifier_1, self.today),
            lambda: start_assigned_stage(user=self.verifier_1, stage_id=first.pk,
                                         started_at=self.today),
        ):
            with self.assertRaises(ValidationError):
                start()
            first.refresh_from_db()
            editorial.refresh_from_db()
            self.assertIsNone(first.started_at)
            self.assertFalse(editorial.is_completed)

    def test_both_entry_points_block_pending_editing(self):
        self.assert_start_blocked('editing')

    def test_both_entry_points_block_pending_author_editing(self):
        self.assert_start_blocked('author_editing')

    def test_direct_post_cannot_bypass_pending_editorial_work(self):
        editorial, first = self.pending_first('editing')
        self.client.force_login(self.verifier_1)
        response = self.client.post(reverse('core:start_first_verification_stage', args=[self.text.pk]),
                                    {'workflow_token': make_token(self.text, self.verifier_1)})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        editorial.refresh_from_db()
        self.assertIsNone(first.started_at)
        self.assertFalse(editorial.is_completed)
        self.assertTrue(any('Tekst nadal znajduje' in str(message)
                            for message in response.wsgi_request._messages))

    def test_completed_editorial_work_allows_first_verification(self):
        editorial, first = self.pending_first('editing')
        editorial.started_at = editorial.ended_at = timezone.localdate()
        editorial.is_completed = True
        editorial.save()
        started = start_first_verification(self.text, self.verifier_1, self.today)
        self.assertEqual(started.pk, first.pk)
        self.assertEqual(started.started_at, self.today)

    def assert_archived_verification_cannot_be_added_again(self, kind, role):
        assignment = A.objects.create(text=self.text, role=role, assigned_to=self.verifier_1,
                                      is_current=False)
        completed = S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                                     started_at=self.today, ended_at=self.today,
                                     is_completed=True, is_current=False)
        for values in ({'historical': True}, {'started_at': self.today, 'ended_at': self.today}):
            with self.assertRaisesMessage(ValidationError, 'już zakończona'):
                add_missing_stage(self.text.pk, self.superuser, version_of(self.text),
                                  kind=kind, performer=self.verifier_2, **values)
            self.assertEqual(S.objects.filter(text=self.text, stage_type=kind).count(), 1)
            self.assertEqual(A.objects.filter(text=self.text, role=role).count(), 1)
            completed.refresh_from_db()
            self.assertEqual(completed.assignment_id, assignment.pk)

    def test_archived_first_is_not_a_missing_stage(self):
        self.assert_archived_verification_cannot_be_added_again('first_verification', 'verifier_1')

    def test_archived_second_is_not_a_missing_stage(self):
        self.assert_archived_verification_cannot_be_added_again('second_verification', 'verifier_2')


class PersonPermissionsAuditTests(WorkflowTestDataMixin, TestCase):
    def permissions(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:person_permissions',
                                           args=[self.editor.person_profile.pk]))
        self.assertEqual(response.status_code, 200)
        return {row['label']: row['allowed'] for row in response.context['permissions']}

    def test_active_editor_keeps_role_restrictions(self):
        permissions = self.permissions()
        labels = dict(A.Role.choices)
        self.assertTrue(permissions[labels[A.Role.EDITOR]])
        for role in (A.Role.VERIFIER_1, A.Role.PROOFREADER_2, A.Role.STYLING):
            self.assertFalse(permissions[labels[role]])

    def test_leave_blocks_all_new_claims_in_permissions(self):
        Vacation.objects.create(person=self.editor.person_profile,
                                start_date=self.today, until_revoked=True)
        self.assertFalse(any(self.permissions().values()))
