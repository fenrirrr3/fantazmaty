from datetime import timedelta
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import _annotated_texts, my_texts_context
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.admin_stage_edit import edit_stage
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.read_queries import annotate_my_work
from workflow.repetitions import repeat_stages
from workflow.services import claim_stage, complete_stage
from workflow.state import current_stage


class StageCompletionCorrectionTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.today = timezone.localdate()
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'test')
        self.member = get_user_model().objects.create_user('member', 'member@example.com', 'test')
        person = Person.objects.create(user=self.member, email=self.member.email, first_name='Jan', last_name='Test')
        person.roles.add(Role.objects.get_or_create(name='Korektor')[0])
        self.book = Anthology.objects.create(title='Test')
        self.text = Text.objects.create(title='Tekst', length=100, anthology=self.book)
        self.assignment = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.member)
        self.stage = S.objects.create(text=self.text, assignment=self.assignment, stage_type='first_proofreading',
                                      started_at=self.today - timedelta(days=10), ended_at=self.today, is_completed=True)
        self.client.force_login(self.admin)

    def reopen(self, actor=None, version=None):
        return edit_stage(self.stage.pk, actor or self.admin,
                          version_of(self.text) if version is None else version, action='reopen')

    def test_reopen_keeps_start_performer_execution_and_becomes_active(self):
        old_version = version_of(self.text)
        original_start = self.stage.started_at
        self.reopen()
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.ended_at)
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.stage.started_at, original_start)
        self.assertEqual(self.stage.assignment_id, self.assignment.pk)
        self.assertEqual(self.stage.execution_number, 1)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)
        self.assertGreater(version_of(self.text), old_version)
        row = annotate_my_work(Text.objects.all(), self.member, self.today).get(pk=self.text.pk)
        self.assertTrue(row.work_active)
        self.assertFalse(row.work_completed)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, self.stage.stage_type)

    def test_empty_continuation_is_retired_and_completion_can_be_saved_again(self):
        pending = S.objects.create(text=self.text, stage_type='second_proofreading')
        self.reopen()
        pending.refresh_from_db()
        self.assertFalse(pending.is_current or pending.is_released)
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, self.stage.pk)
        self.stage.refresh_from_db()
        complete_stage(self.stage, self.member, self.today)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.ended_at, self.today)
        self.assertEqual(S.objects.current_cycle().filter(text=self.text, stage_type='second_proofreading').count(), 1)

    def test_later_started_reserved_or_completed_work_blocks_and_preserves_both_rows(self):
        for state in ('started', 'reserved', 'completed'):
            with self.subTest(state=state):
                assignment = A.objects.create(text=self.text, role='proofreader_2', assigned_to=self.member if state == 'reserved' else None)
                later = S.objects.create(text=self.text, stage_type='second_proofreading', assignment=assignment,
                                         started_at=self.today if state != 'reserved' else None,
                                         ended_at=self.today if state == 'completed' else None, is_completed=state == 'completed')
                with self.assertRaisesMessage(ValidationError, 'Dalsza praca'):
                    self.reopen()
                self.stage.refresh_from_db()
                later.refresh_from_db()
                self.assertTrue(self.stage.is_completed)
                self.assertTrue(later.is_current)
                later.delete()
                assignment.delete()

    def test_closed_anthology_stale_request_and_non_superuser_block_correction(self):
        with self.assertRaises(PermissionDenied):
            self.reopen(actor=self.member)
        with self.assertRaises(ValidationError):
            self.reopen(version=version_of(self.text) - 1)
        S.objects.create(text=self.text, stage_type='ready')
        self.book.status = Anthology.Status.READY
        self.book.save()
        with self.assertRaisesMessage(ValidationError, 'Antologia jest gotowa'):
            self.reopen()
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)

    def test_admin_inline_link_and_post_reopen(self):
        change_url = reverse('admin:texts_text_change', args=[self.text.pk])
        response = self.client.get(change_url)
        self.assertContains(response, '?action=reopen')
        self.assertContains(response, 'Cofnij zakończenie')
        url = reverse('admin:workflow_stage_correct', args=[self.stage.pk])
        response = self.client.get(url, {'action': 'reopen'})
        self.assertEqual(response.context['form'].initial['action'], 'reopen')
        response = self.client.post(url, {'action': 'reopen', 'version': version_of(self.text), 'confirm': 'on'})
        self.assertRedirects(response, change_url, fetch_redirect_response=False)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        response = self.client.get(change_url)
        self.assertNotContains(response, '?action=reopen')

    def test_imported_stage_with_start_loses_completed_import_exception(self):
        token = importing_completed.set(True)
        try:
            self.stage.imported_completed = True
            self.stage.save()
        finally:
            importing_completed.reset(token)
        start = self.stage.started_at
        self.reopen()
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.imported_completed)
        self.assertFalse(importing_completed.get())
        self.assertEqual(self.stage.started_at, start)
        self.stage.full_clean()

    def test_imported_stage_without_start_shows_form_error_without_guessing_date(self):
        token = importing_completed.set(True)
        try:
            self.stage.imported_completed = True
            self.stage.started_at = None
            self.stage.ended_at = None
            self.stage.save()
        finally:
            importing_completed.reset(token)
        response = self.client.post(reverse('admin:workflow_stage_correct', args=[self.stage.pk]),
                                    {'action': 'reopen', 'version': version_of(self.text), 'confirm': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Najpierw uzupełnij ją')
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed and self.stage.imported_completed)
        self.assertIsNone(self.stage.started_at)

    def test_archived_execution_and_withdrawn_text_cannot_reopen(self):
        self.stage.is_current = False
        self.stage.save()
        with self.assertRaises(ValidationError):
            self.reopen()
        self.stage.is_current = True
        self.stage.save()
        S.objects.create(text=self.text, stage_type='withdrawn')
        with self.assertRaisesMessage(ValidationError, 'Tekst jest wycofany'):
            self.reopen()

    def test_ready_marker_is_retired_when_last_work_is_reopened(self):
        assignment = A.objects.create(text=self.text, role='styling', assigned_to=self.admin)
        self.stage.stage_type = 'styling'
        self.stage.assignment = assignment
        self.stage.save()
        marker = S.objects.create(text=self.text, stage_type='ready', started_at=self.today)
        self.reopen()
        marker.refresh_from_db()
        self.assertFalse(marker.is_current)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'styling')

    def test_last_stage_of_completed_repeat_can_reopen_and_finish_same_execution(self):
        S.objects.create(text=self.text, stage_type='ready')
        # A later, unselected completed checkpoint must remain unchanged.
        later = S.objects.create(text=self.text, stage_type='third_proofreading', started_at=self.today,
                                 ended_at=self.today, is_completed=True)
        run = repeat_stages(self.text, ['first_proofreading'], self.admin)
        self.stage = claim_stage(self.text, 'first_proofreading', self.member)
        complete_stage(self.stage, self.member, self.today)
        original_pk = self.stage.pk
        original_execution = self.stage.execution_number
        self.reopen()
        run.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNone(run.completed_at)
        self.assertTrue(later.is_current and later.is_completed)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.stage.pk, original_pk)
        self.assertEqual(self.stage.execution_number, original_execution)
        complete_stage(self.stage, self.member, self.today)
        run.refresh_from_db()
        self.assertIsNotNone(run.completed_at)
        self.assertEqual(run.stages.count(), 1)


class MyTextsAllOrderingTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.member = get_user_model().objects.create_user('member', 'member@example.com', 'test')
        person = Person.objects.create(user=self.member, email=self.member.email, first_name='Jan', last_name='Test')
        person.roles.add(Role.objects.get_or_create(name='Korektor')[0])
        self.other = get_user_model().objects.create_user('other', 'other@example.com', 'test')
        self.book = Anthology.objects.create(title='Test')

    def work(self, title, ago=None, editor=False, control_ago=None):
        text = Text.objects.create(title=title, length=100, anthology=self.book)
        assignment = A.objects.create(text=text, role='editor' if editor else 'proofreader_1', assigned_to=self.member)
        ended = self.today - timedelta(days=ago) if ago is not None else None
        token = importing_completed.set(True)
        try:
            S.objects.create(text=text, stage_type='editing' if editor else 'first_proofreading', assignment=assignment,
                             started_at=ended, ended_at=ended, is_completed=True, imported_completed=ago is None)
        finally:
            importing_completed.reset(token)
        if editor:
            control_end = self.today - timedelta(days=control_ago) if control_ago is not None else self.today
            S.objects.create(text=text, stage_type='editing_control', started_at=control_end, ended_at=control_end, is_completed=True)
        return text

    def rows(self, params=''):
        return list(my_texts_context(user=self.member, selected_view='all', params=QueryDict(params))['texts'])

    def test_all_uses_title_order_including_unknown_completion_dates(self):
        oldest = self.work('A dawny', 20)
        newest = self.work('Z nowy', 1)
        unknown = self.work('B bez daty')
        middle = self.work('C średni', 10)
        self.assertEqual([r['pk'] for r in self.rows()], [oldest.pk, unknown.pk, middle.pk, newest.pk])

    def test_all_includes_editor_waiting_after_control_and_completed_proofreader(self):
        editor = self.work('A redakcja', 30, editor=True, control_ago=1)
        proof = self.work('Z korekta', 5)
        self.assertEqual([r['pk'] for r in self.rows()], [editor.pk, proof.pk])

    def test_other_performers_completion_does_not_change_title_order(self):
        old = self.work('A dawny', 20)
        recent = self.work('B nowy', 5)
        other_assignment = A.objects.create(text=old, role='proofreader_2', assigned_to=self.other)
        S.objects.create(text=old, stage_type='second_proofreading', assignment=other_assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertEqual([r['pk'] for r in self.rows()], [old.pk, recent.pk])

    def test_several_own_roles_keep_single_row_and_explicit_header_sort(self):
        both = self.work('A dwa zadania', 20)
        recent = self.work('Z nowe', 5)
        assignment = A.objects.create(text=both, role='proofreader_3', assigned_to=self.member)
        S.objects.create(text=both, stage_type='third_proofreading', assignment=assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertEqual([r['pk'] for r in self.rows()], [both.pk, recent.pk])
        self.assertEqual([r['pk'] for r in self.rows('sort=-title')], [recent.pk, both.pk])

    def test_default_order_is_preserved_through_pagination(self):
        texts = [self.work(f'{number:02}', 30 - number) for number in range(30)]
        self.client.force_login(self.member)
        url = reverse('core:my_texts')
        first = self.client.get(url, {'view': 'all', 'page_size': 25})
        second = self.client.get(url, {'view': 'all', 'page_size': 25, 'page': 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        actual = [row['pk'] for response in (first, second) for row in response.context['texts']]
        self.assertEqual(actual, [text.pk for text in texts])
