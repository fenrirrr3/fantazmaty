"""Korekta pojedynczego przypisania w adminie (zmiana osoby, odłączenie, usunięcie)."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Text
from workflow.admin_assignment_edit import correct_assignment
from workflow.models import WorkflowHandoff, WorkflowRepetition, WorkflowRoleAssignment as A, WorkflowStage as S


class AssignmentCorrectionTests(TestCase):
    """correct_assignment: uprawnienia, aktualność formularza, ochrona historii i skutki dla etapów."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Redaktor')
        cls.other = create_member('other', 'Redaktor')
        cls.book = Anthology.objects.create(title='Test')
        cls.today = timezone.localdate()

    def setUp(self):
        self.text = Text.objects.create(title='Tekst', length=100, anthology=self.book)
        self.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)

    def correct(self, action, performer=None, actor=None, version=None):
        return correct_assignment(self.assignment.pk, actor or self.admin,
                                  version_of(self.text) if version is None else version,
                                  action=action, performer=performer)

    def test_only_superuser_with_current_form_can_correct(self):
        with self.assertRaises(PermissionDenied):
            self.correct('performer', self.other, actor=self.member)
        with self.assertRaisesMessage(ValidationError, 'Dane zmieniły się'):
            self.correct('performer', self.other, version=version_of(self.text) - 1)
        with self.assertRaisesMessage(ValidationError, 'Nieprawidłowa operacja'):
            self.correct('merge')
        with self.assertRaisesMessage(ValidationError, 'Wybierz wykonawcę'):
            self.correct('performer')
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to, self.member)

    def test_repetition_and_handoff_history_cannot_be_rewritten(self):
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=self.assignment,
                                 started_at=self.today)
        run = WorkflowRepetition.objects.create(text=self.text, completed_at=timezone.now(),
                                                previous_assignment_ids=[self.assignment.pk])
        with self.assertRaisesMessage(ValidationError, 'historii powtórzeń'):
            self.correct('performer', self.other)
        run.delete()
        replacement = A.objects.create(text=self.text, role='editor', assigned_to=self.other,
                                       execution_number=2, is_current=False)
        WorkflowHandoff.objects.create(text=self.text, stage=stage, previous_assignment=self.assignment,
                                       new_assignment=replacement, actor=self.admin, reason='Test')
        for action in ('performer', 'clear', 'delete'):
            with self.subTest(action=action), self.assertRaisesMessage(ValidationError, 'historię przekazania'):
                self.correct(action, self.other)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to, self.member)

    def test_delete_removes_only_an_unused_assignment(self):
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=self.assignment)
        with self.assertRaisesMessage(ValidationError, 'nadal jest używane'):
            self.correct('delete')
        stage.delete()
        before = version_of(self.text)
        self.assertEqual(self.correct('delete'), self.text.pk)
        self.assertFalse(A.objects.filter(pk=self.assignment.pk).exists())
        self.assertGreater(version_of(self.text), before)

    def test_unused_assignment_gets_new_person_and_same_person_is_a_no_op(self):
        known_date = self.assignment.assigned_at
        before = version_of(self.text)
        self.correct('performer', self.member)
        self.assertEqual(version_of(self.text), before)
        self.correct('performer', self.other)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to, self.other)
        self.assertEqual(self.assignment.assigned_at, known_date)
        self.assertGreater(version_of(self.text), before)

    def test_clear_returns_unfinished_work_to_the_queue(self):
        future = self.today + timedelta(days=3)
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=self.assignment,
                                 started_at=future)
        self.correct('clear')
        self.assignment.refresh_from_db()
        stage.refresh_from_db()
        self.assertIsNone(self.assignment.assigned_to_id)
        self.assertIsNone(stage.started_at)
        self.assertEqual(stage.assignment_id, self.assignment.pk)
        self.assertEqual(stage.waiting_reset_at, self.today)


class AssignmentCorrectionPageTests(TestCase):
    """Formularz korekty przypisania: dostęp, lista etapów, błędy i zapis z przekierowaniem."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.staff = create_member('staff', 'Redaktor')
        cls.text = Text.objects.create(title='Tekst', length=100)
        cls.assignment = A.objects.create(text=cls.text, role='editor', assigned_to=cls.staff)
        cls.stage = S.objects.create(text=cls.text, stage_type='editing', assignment=cls.assignment)
        cls.url = reverse('admin:workflow_assignment_correct', args=[cls.assignment.pk])

    def post(self, action, **extra):
        return self.client.post(self.url, {'action': action, 'version': version_of(self.text),
                                           'confirm': 'on', **extra})

    def test_page_lists_linked_stages_and_reports_refused_deletion(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertContains(page, reverse('admin:workflow_stage_correct', args=[self.stage.pk]))
        response = self.post('delete')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nadal jest używane')
        self.assertTrue(A.objects.filter(pk=self.assignment.pk).exists())

    def test_clear_redirects_to_text_and_member_is_sent_to_admin_login(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(self.url), reverse('admin:login') + '?next=' + self.url,
                             fetch_redirect_response=False)
        self.client.force_login(self.admin)
        response = self.post('clear')
        self.assertRedirects(response, reverse('admin:texts_text_change', args=[self.text.pk]),
                             fetch_redirect_response=False)
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.assigned_to_id)
