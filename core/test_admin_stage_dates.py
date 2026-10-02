from datetime import timedelta
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.models import WorkflowEvent
from core.selectors.texts import text_detail_context
from core.services.texts import start_assigned_stage
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.admin_stage_dates import StageDatesForm, set_stage_dates
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.repetitions import repeat_stages
from workflow.services import user_can_complete_stage


class AdminStageDatesTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=10)
        self.end = self.today - timedelta(days=2)
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'test')
        self.member = get_user_model().objects.create_user('member', 'member@example.com', 'test')
        person = Person.objects.create(user=self.member, email=self.member.email)
        for role in ('Korektor', 'Redaktor', 'Weryfikator'):
            person.roles.add(Role.objects.get_or_create(name=role)[0])
        self.book = Anthology.objects.create(title='Test')
        self.text = Text.objects.create(title='Tekst', length=100, anthology=self.book)
        self.assignment = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.member)
        self.stage = S.objects.create(text=self.text, assignment=self.assignment,
                                      stage_type='first_proofreading', started_at=self.start)
        self.client.force_login(self.admin)

    def save_dates(self, **kwargs):
        data = {'started_at': self.start, 'ended_at': None, **kwargs}
        return set_stage_dates(self.stage.pk, self.admin, version_of(self.text), **data)

    def test_manual_start_correction_keeps_execution_performer_and_status(self):
        corrected = self.start + timedelta(days=1)
        self.save_dates(started_at=corrected)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, corrected)
        self.assertFalse(self.stage.is_completed)
        self.assertIsNone(self.stage.ended_at)
        self.assertEqual(self.stage.assignment_id, self.assignment.pk)
        self.assertEqual(self.stage.execution_number, 1)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_finish_uses_manual_dates_creates_next_and_records_actual_admin(self):
        self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.ended_at, self.end)
        pending = S.objects.current_cycle().get(text=self.text, stage_type='second_proofreading')
        self.assertIsNone(pending.started_at)
        self.assertFalse(pending.is_completed)
        event = WorkflowEvent.objects.get(text=self.text)
        self.assertEqual(event.actor_id, self.admin.pk)
        self.assertIn('Zakończono etap', event.details)

    def test_unstarted_work_can_be_started_and_finished_with_past_dates(self):
        self.stage.started_at = None
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)
        self.assertTrue(self.stage.is_completed)

    def test_start_only_accepts_past_date_in_admin(self):
        self.stage.started_at = None
        self.stage.save()
        self.save_dates()
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_past_date_override_is_unavailable_to_regular_member(self):
        self.stage.started_at = None
        self.stage.save()
        with self.assertRaises(PermissionDenied):
            start_assigned_stage(user=self.member, stage_id=self.stage.pk,
                                 started_at=self.start, allow_past=True)
        with self.assertRaises(ValidationError):
            start_assigned_stage(user=self.member, stage_id=self.stage.pk, started_at=self.start)
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.started_at)

    def test_invalid_completion_rolls_back_start_correction_and_successor(self):
        for end in (self.start - timedelta(days=1), self.today + timedelta(days=1)):
            with self.subTest(end=end), self.assertRaises(ValidationError):
                self.save_dates(started_at=self.start + timedelta(days=1), ended_at=end, finish=True)
            self.stage.refresh_from_db()
            self.assertEqual(self.stage.started_at, self.start)
            self.assertFalse(self.stage.is_completed)
            self.assertEqual(S.objects.filter(text=self.text).count(), 1)
            self.assertFalse(WorkflowEvent.objects.filter(text=self.text).exists())

    def test_end_without_transition_and_missing_dates_are_rejected(self):
        for data in ({'ended_at': self.end}, {'finish': True},
                     {'started_at': None, 'ended_at': self.end, 'finish': True}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                self.save_dates(**data)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_stale_and_duplicate_completions_do_not_create_another_successor(self):
        version = version_of(self.text)
        self.save_dates(ended_at=self.end, finish=True)
        with self.assertRaises(ValidationError):
            set_stage_dates(self.stage.pk, self.admin, version, started_at=self.start,
                            ended_at=self.end, finish=True)
        with self.assertRaises(ValidationError):
            self.save_dates(ended_at=self.end, finish=True)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='second_proofreading').count(), 1)

    def test_permission_closed_anthology_and_withdrawal_block_changes(self):
        with self.assertRaises(PermissionDenied):
            set_stage_dates(self.stage.pk, self.member, version_of(self.text), started_at=self.start)
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        marker = S.objects.create(text=self.text, stage_type='ready')
        self.book.status = Anthology.Status.READY
        self.book.save()
        with self.assertRaisesMessage(ValidationError, 'Antologia jest gotowa'):
            self.save_dates(ended_at=self.end, finish=True)
        self.book.status = Anthology.Status.IN_PREPARATION
        self.book.save()
        marker.delete()
        self.stage.is_completed = False
        self.stage.ended_at = None
        self.stage.save()
        S.objects.create(text=self.text, stage_type='withdrawn')
        with self.assertRaises(ValidationError):
            self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_archived_unfinished_stage_cannot_be_finished(self):
        self.stage.is_current = False
        self.stage.save()
        with self.assertRaises(ValidationError):
            self.save_dates(ended_at=self.end, finish=True)

    def test_completed_history_date_correction_does_not_create_work(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.is_current = False
        self.stage.save()
        corrected = self.end - timedelta(days=1)
        self.save_dates(ended_at=corrected)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertFalse(self.stage.is_current)
        self.assertEqual(self.stage.ended_at, corrected)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_imported_history_allows_unknown_dates_and_later_filling_them(self):
        token = importing_completed.set(True)
        try:
            self.stage.imported_completed = True
            self.stage.is_completed = True
            self.stage.started_at = None
            self.stage.save()
        finally:
            importing_completed.reset(token)
        self.save_dates(started_at=None)
        self.save_dates(ended_at=self.end)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed and self.stage.imported_completed)
        self.assertEqual(self.stage.ended_at, self.end)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)

    def test_start_cannot_precede_previous_completed_work(self):
        S.objects.create(text=self.text, stage_type='editing_control', started_at=self.start,
                         ended_at=self.start + timedelta(days=2), is_completed=True)
        with self.assertRaises(ValidationError):
            self.save_dates(started_at=self.start + timedelta(days=1))
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, self.start)

    def completed_neighbours(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        # Creation order is deliberately different from workflow order.
        following = S.objects.create(text=self.text, stage_type='second_proofreading',
            started_at=self.end + timedelta(days=1), ended_at=self.today, is_completed=True)
        previous = S.objects.create(text=self.text, stage_type='editing_control',
            started_at=self.start - timedelta(days=4),
            ended_at=self.start - timedelta(days=2), is_completed=True)
        return previous, following

    def assert_completed_correction_rejected(self, **dates):
        stages = list(S.objects.filter(text=self.text).order_by('pk').values())
        assignments = list(A.objects.filter(text=self.text).order_by('pk').values())
        events = list(WorkflowEvent.objects.filter(text=self.text).order_by('pk').values())
        version = version_of(self.text)
        with self.assertRaises(ValidationError):
            self.save_dates(**dates)
        self.assertEqual(list(S.objects.filter(text=self.text).order_by('pk').values()), stages)
        self.assertEqual(list(A.objects.filter(text=self.text).order_by('pk').values()), assignments)
        self.assertEqual(list(WorkflowEvent.objects.filter(text=self.text).order_by('pk').values()), events)
        self.assertEqual(version_of(self.text), version)

    def test_completed_start_cannot_precede_previous_completed_end(self):
        previous, _ = self.completed_neighbours()
        for current in (True, False):
            with self.subTest(current=current):
                self.stage.is_current = current
                self.stage.save()
                self.assert_completed_correction_rejected(
                    started_at=previous.ended_at - timedelta(days=1), ended_at=self.end)

    def test_completed_end_cannot_follow_next_completed_start(self):
        _, following = self.completed_neighbours()
        for current in (True, False):
            with self.subTest(current=current):
                self.stage.is_current = current
                self.stage.save()
                # Still within the next stage's interval; comparing ends is insufficient.
                self.assert_completed_correction_rejected(ended_at=following.ended_at)

    def test_completed_correction_accepts_same_day_handoffs_without_new_work(self):
        previous, following = self.completed_neighbours()
        identity = (self.stage.assignment_id, self.stage.execution_number, self.stage.iteration)
        self.save_dates(started_at=previous.ended_at, ended_at=following.started_at)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.started_at, previous.ended_at)
        self.assertEqual(self.stage.ended_at, following.started_at)
        self.assertTrue(self.stage.is_completed and self.stage.is_current)
        self.assertEqual((self.stage.assignment_id, self.stage.execution_number,
                          self.stage.iteration), identity)
        self.assertEqual(S.objects.filter(text=self.text).count(), 3)

    def test_completed_correction_respects_other_iterations_of_the_same_stage(self):
        self.stage.iteration = self.stage.execution_number = 2
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        previous = S.objects.create(text=self.text, stage_type=self.stage.stage_type,
            started_at=self.start - timedelta(days=3),
            ended_at=self.start - timedelta(days=1), is_completed=True)
        following = S.objects.create(text=self.text, stage_type=self.stage.stage_type,
            iteration=3, execution_number=3, started_at=self.end + timedelta(days=1),
            ended_at=self.today, is_completed=True)
        self.assert_completed_correction_rejected(
            started_at=previous.ended_at - timedelta(days=1), ended_at=self.end)
        self.assert_completed_correction_rejected(ended_at=following.ended_at)
        self.save_dates(started_at=previous.ended_at, ended_at=following.started_at)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.execution_number, 2)

    def test_completed_end_cannot_follow_started_successor(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        S.objects.create(text=self.text, stage_type='second_proofreading', started_at=self.end)
        self.assert_completed_correction_rejected(ended_at=self.today)

    def test_completed_correction_does_not_compare_other_texts(self):
        previous, following = self.completed_neighbours()
        other = Text.objects.create(title='Inny tekst', length=100, anthology=self.book)
        S.objects.create(text=other, stage_type='editing_control',
                         started_at=self.today, ended_at=self.today, is_completed=True)
        S.objects.create(text=other, stage_type='second_proofreading',
            started_at=self.start - timedelta(days=20),
            ended_at=self.start - timedelta(days=10), is_completed=True)
        self.save_dates(started_at=previous.ended_at, ended_at=following.started_at)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.ended_at, following.started_at)

    def test_completed_history_correction_uses_its_own_cycle(self):
        previous, following = self.completed_neighbours()
        S.objects.filter(text=self.text).update(is_current=False)
        self.text.current_workflow_cycle = 2
        self.text.save()
        S.objects.create(text=self.text, workflow_cycle=2, stage_type='editing_control',
                         started_at=self.end, ended_at=self.today, is_completed=True)
        S.objects.create(text=self.text, workflow_cycle=2, stage_type='second_proofreading',
            started_at=self.start - timedelta(days=20),
            ended_at=self.start - timedelta(days=10), is_completed=True)
        self.assert_completed_correction_rejected(
            started_at=previous.ended_at - timedelta(days=1), ended_at=self.end)
        self.assert_completed_correction_rejected(ended_at=following.ended_at)
        self.save_dates(started_at=previous.ended_at, ended_at=following.started_at)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_current)
        self.assertEqual(self.stage.workflow_cycle, 1)
        self.assertEqual(self.stage.ended_at, following.started_at)

    def test_imported_completed_correction_checks_known_neighbour_boundaries(self):
        previous, following = self.completed_neighbours()
        token = importing_completed.set(True)
        try:
            for stage in (previous, self.stage, following):
                stage.imported_completed = True
            previous.started_at = None
            self.stage.started_at = self.stage.ended_at = None
            following.ended_at = None
            for stage in (previous, self.stage, following):
                stage.save()
        finally:
            importing_completed.reset(token)
        self.assert_completed_correction_rejected(
            started_at=previous.ended_at - timedelta(days=1), ended_at=self.end)
        self.assert_completed_correction_rejected(started_at=None, ended_at=self.today)
        for dates in ({'started_at': None, 'ended_at': self.end},
                      {'started_at': self.start, 'ended_at': None},
                      {'started_at': None, 'ended_at': None},
                      {'started_at': self.start, 'ended_at': self.end}):
            with self.subTest(dates=dates):
                self.save_dates(**dates)
                self.stage.refresh_from_db()
                self.assertTrue(self.stage.is_completed and self.stage.imported_completed)
                self.assertEqual(self.stage.started_at, dates['started_at'])
                self.assertEqual(self.stage.ended_at, dates['ended_at'])
        self.assertEqual(S.objects.filter(text=self.text).count(), 3)

    def test_completed_correction_ignores_unknown_neighbour_boundaries(self):
        previous, following = self.completed_neighbours()
        token = importing_completed.set(True)
        try:
            previous.imported_completed = following.imported_completed = True
            previous.ended_at = following.started_at = None
            previous.save()
            following.save()
        finally:
            importing_completed.reset(token)
        self.save_dates(ended_at=self.end)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.ended_at, self.end)

    def test_completed_repetition_correction_uses_queue_and_retired_predecessors(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.start + timedelta(days=2)
        self.stage.save()
        styling = S.objects.create(text=self.text, stage_type='styling',
            started_at=self.stage.ended_at, ended_at=self.start + timedelta(days=3), is_completed=True)
        S.objects.create(text=self.text, stage_type='ready', started_at=styling.ended_at)
        run = repeat_stages(self.text, ['first_proofreading', 'styling'], self.admin)
        repeated = run.stages.get(stage_type='first_proofreading')
        following = run.stages.get(stage_type='styling')
        for stage, performer, offset in ((repeated, self.member, 4), (following, self.admin, 6)):
            stage.assignment.assigned_to = performer
            stage.assignment.save()
            set_stage_dates(stage.pk, self.admin, version_of(self.text),
                started_at=self.start + timedelta(days=offset),
                ended_at=self.start + timedelta(days=offset + 1), finish=True)
            stage.refresh_from_db()
        self.stage = repeated
        self.assert_completed_correction_rejected(
            started_at=styling.ended_at - timedelta(days=1), ended_at=repeated.ended_at)
        self.assert_completed_correction_rejected(
            started_at=repeated.started_at, ended_at=following.ended_at)
        self.save_dates(started_at=styling.ended_at, ended_at=following.started_at)
        repeated.refresh_from_db()
        self.assertTrue(repeated.is_completed and repeated.is_current)
        self.assertEqual(repeated.execution_number, 2)
        self.assertEqual(S.objects.filter(text=self.text).count(), 6)

    def test_completed_editing_return_correction_respects_verification_and_author(self):
        self.stage.stage_type = 'editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        self.save_dates(ended_at=self.start + timedelta(days=1), finish=True,
                        next_stage='first_verification')
        verification = S.objects.get(text=self.text, stage_type='first_verification')
        verification.assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.admin)
        verification.save()
        set_stage_dates(verification.pk, self.admin, version_of(self.text),
            started_at=self.start + timedelta(days=1), ended_at=self.start + timedelta(days=2), finish=True)
        verification.refresh_from_db()
        self.stage = S.objects.get(text=self.text, stage_type='editing', iteration=2)
        editing_end = self.start + timedelta(days=4)
        author_end = self.start + timedelta(days=6)
        self.save_dates(started_at=verification.ended_at, ended_at=editing_end,
                        finish=True, next_stage='author_editing')
        author = S.objects.get(text=self.text, stage_type='author_editing')
        set_stage_dates(author.pk, self.admin, version_of(self.text),
                        started_at=editing_end, ended_at=author_end, finish=True)
        self.assert_completed_correction_rejected(
            started_at=verification.ended_at - timedelta(days=1), ended_at=editing_end)
        self.assert_completed_correction_rejected(started_at=verification.ended_at, ended_at=author_end)
        self.save_dates(started_at=verification.ended_at, ended_at=editing_end)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.iteration, 2)
        # A second author exchange must bracket the intervening editorial pass.
        self.stage = S.objects.get(text=self.text, stage_type='editing', iteration=3)
        self.save_dates(started_at=author_end, ended_at=self.end,
                        finish=True, next_stage='author_editing')
        following = S.objects.get(text=self.text, stage_type='author_editing', iteration=2)
        set_stage_dates(following.pk, self.admin, version_of(self.text),
                        started_at=self.end, ended_at=self.today, finish=True)
        self.assert_completed_correction_rejected(
            started_at=author_end - timedelta(days=1), ended_at=self.end)
        self.assert_completed_correction_rejected(started_at=author_end, ended_at=self.today)
        self.save_dates(started_at=author_end, ended_at=self.end)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.iteration, 3)

    def test_admin_links_and_date_form_post(self):
        url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])
        for route, pk in (('admin:texts_text_change', self.text.pk),
                          ('admin:workflow_workflowstage_change', self.stage.pk),
                          ('admin:workflow_workflowstage_changelist', None)):
            response = self.client.get(reverse(route, args=[pk] if pk else None))
            self.assertContains(response, url)
            self.assertContains(response, 'Ustaw daty / zakończ etap')
        response = self.client.get(url)
        self.assertContains(response, 'type="date"')
        self.assertContains(response, self.start.isoformat())
        response = self.client.post(url, {'started_at': self.start.isoformat(),
            'ended_at': self.end.isoformat(), 'finish': 'on', 'version': version_of(self.text)})
        self.assertRedirects(response, reverse('admin:texts_text_change', args=[self.text.pk]),
                             fetch_redirect_response=False)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)

    def test_admin_form_displays_errors_without_partial_changes(self):
        url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])
        response = self.client.post(url, {'started_at': self.start.isoformat(),
            'ended_at': (self.start - timedelta(days=1)).isoformat(),
            'finish': 'on', 'version': version_of(self.text)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Data zakończenia nie może być wcześniejsza')
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_editor_control_can_be_started_and_completed_by_admin_for_assigned_editor(self):
        self.stage.stage_type = 'editor_control'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.started_at = None
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.assignment.assigned_to_id, self.member.pk)
        self.assertTrue(S.objects.filter(text=self.text, stage_type='third_proofreading').exists())

    def test_editor_control_still_rejects_unassigned_coordinator(self):
        self.stage.stage_type = 'editor_control'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        other = get_user_model().objects.create_user('other', 'other@example.com', 'test')
        person = Person.objects.create(user=other, email=other.email)
        person.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        self.assertFalse(user_can_complete_stage(self.stage, other))

    def test_normal_editing_transitions_use_existing_checkpoints(self):
        for destination in ('first_verification', 'author_editing', 'second_verification', 'editing_control'):
            with self.subTest(destination=destination):
                text = Text.objects.create(title=destination, length=100, anthology=self.book)
                assignment = A.objects.create(text=text, role='editor', assigned_to=self.member)
                for checkpoint in (['first_verification'] if destination in ('author_editing', 'second_verification')
                                   else ['first_verification', 'second_verification'] if destination == 'editing_control' else []):
                    S.objects.create(text=text, stage_type=checkpoint, started_at=self.start,
                                     ended_at=self.start, is_completed=True)
                editing = S.objects.create(text=text, stage_type='editing', assignment=assignment, started_at=self.start)
                set_stage_dates(editing.pk, self.admin, version_of(text), started_at=self.start,
                                ended_at=self.end, finish=True, next_stage=destination)
                editing.refresh_from_db()
                self.assertTrue(editing.is_completed)
                self.assertEqual(editing.ended_at, self.end)
                self.assertTrue(S.objects.current_cycle().filter(text=text, stage_type=destination,
                                                                 is_completed=False).exists())

    def test_invalid_editing_checkpoint_rolls_back_date_change(self):
        self.stage.stage_type = 'editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        with self.assertRaises(ValidationError):
            self.save_dates(started_at=self.start + timedelta(days=1), ended_at=self.end,
                            finish=True, next_stage='editing_control')
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.stage.started_at, self.start)

    def imported_editing_checkpoints(self, *kinds):
        self.stage.stage_type = 'editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        imported = []
        token = importing_completed.set(True)
        try:
            for kind in kinds:
                role = 'verifier_1' if kind == 'first_verification' else 'verifier_2'
                performer = self.member if role == 'verifier_1' else self.admin
                assignment = A.objects.create(text=self.text, role=role, assigned_to=performer)
                stage = S(text=self.text, stage_type=kind, assignment=assignment,
                          is_completed=True, imported_completed=True)
                stage.full_clean()
                stage.save()
                imported.append(stage)
        finally:
            importing_completed.reset(token)
        return imported

    def test_dateless_completed_first_verification_is_not_offered_again(self):
        first, = self.imported_editing_checkpoints('first_verification')
        form = StageDatesForm(stage=self.stage)
        choices = dict(form.fields['next_stage'].choices)
        self.assertNotIn('first_verification', choices)
        self.assertIn('author_editing', choices)
        self.assertIn('second_verification', choices)
        self.assertNotIn('editing_control', choices)
        detail = text_detail_context(user=self.admin, text=self.text)
        self.assertTrue(detail['first_verification_completed'])
        self.assertFalse(detail['can_send_to_first_verification'])
        self.assertTrue(detail['can_send_to_second_verification'])
        with self.assertRaisesMessage(ValidationError, 'Pierwsza weryfikacja jest już zakończona'):
            self.save_dates(ended_at=self.end, finish=True, next_stage='first_verification')
        self.save_dates(ended_at=self.end, finish=True, next_stage='second_verification')
        first.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.ended_at)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)
        self.assertTrue(S.objects.filter(text=self.text, stage_type='second_verification', is_completed=False).exists())

    def test_dateless_completed_second_verification_unlocks_coordinator(self):
        imported = self.imported_editing_checkpoints('first_verification', 'second_verification')
        form = StageDatesForm(stage=self.stage)
        choices = dict(form.fields['next_stage'].choices)
        self.assertNotIn('first_verification', choices)
        self.assertNotIn('second_verification', choices)
        self.assertIn('editing_control', choices)
        detail = text_detail_context(user=self.admin, text=self.text)
        self.assertTrue(detail['second_verification_completed'])
        self.assertTrue(detail['can_finish_editing'])
        self.save_dates(ended_at=self.end, finish=True, next_stage='editing_control')
        self.assertTrue(S.objects.filter(text=self.text, stage_type='editing_control', is_completed=False).exists())
        for stage in imported:
            stage.refresh_from_db()
            self.assertTrue(stage.is_completed)
            self.assertIsNone(stage.started_at)
            self.assertIsNone(stage.ended_at)

    def test_admin_rejects_post_of_completed_dateless_verification(self):
        self.imported_editing_checkpoints('first_verification')
        url = reverse('admin:workflow_stage_dates', args=[self.stage.pk])
        response = self.client.get(url)
        self.assertNotContains(response, '<option value="first_verification">')
        self.assertContains(response, '<option value="second_verification">')
        response = self.client.post(url, {'started_at': self.start.isoformat(),
            'ended_at': self.end.isoformat(), 'finish': 'on', 'next_stage': 'first_verification',
            'version': version_of(self.text)})
        self.assertEqual(response.status_code, 200)
        self.assertIn('next_stage', response.context['form'].errors)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)

    def test_old_cycle_or_retired_verification_does_not_block_new_workflow(self):
        first, = self.imported_editing_checkpoints('first_verification')
        first.is_current = False
        first.save()
        choices = dict(StageDatesForm(stage=self.stage).fields['next_stage'].choices)
        self.assertIn('first_verification', choices)
        self.assertNotIn('second_verification', choices)
        first.is_current = True
        first.workflow_cycle = self.text.current_workflow_cycle + 1
        first.save()
        choices = dict(StageDatesForm(stage=self.stage).fields['next_stage'].choices)
        self.assertIn('first_verification', choices)

    def test_author_completion_resumes_editing_on_manual_end_date(self):
        self.stage.stage_type = 'author_editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True)
        editing = S.objects.current_cycle().get(text=self.text, stage_type='editing')
        self.assertEqual(editing.started_at, self.end)
        self.assertFalse(editing.is_completed)

    def test_ready_for_editing_start_becomes_editing_and_can_be_forwarded(self):
        self.stage.stage_type = 'ready_for_editing'
        self.stage.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage.started_at = None
        self.stage.save()
        self.save_dates(ended_at=self.end, finish=True, next_stage='first_verification')
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.stage_type, 'editing')
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(S.objects.current_cycle().filter(text=self.text, stage_type='first_verification').count(), 1)

    def test_repetition_releases_next_selected_stage_without_regular_successor(self):
        self.stage.is_completed = True
        self.stage.ended_at = self.end
        self.stage.save()
        S.objects.create(text=self.text, stage_type='styling', started_at=self.start,
                         ended_at=self.end, is_completed=True)
        S.objects.create(text=self.text, stage_type='ready', started_at=self.end)
        run = repeat_stages(self.text, ['first_proofreading', 'styling'], self.admin)
        repeated = run.stages.get(stage_type='first_proofreading')
        blocked = run.stages.get(stage_type='styling')
        with self.assertRaises(ValidationError):
            set_stage_dates(blocked.pk, self.admin, version_of(self.text), started_at=self.today,
                            ended_at=self.today, finish=True)
        repeated.assignment.assigned_to = self.member
        repeated.assignment.save()
        set_stage_dates(repeated.pk, self.admin, version_of(self.text), started_at=self.today,
                        ended_at=self.today, finish=True)
        blocked.refresh_from_db()
        self.assertTrue(blocked.is_released)
        self.assertFalse(S.objects.current_cycle().filter(text=self.text, stage_type='second_proofreading').exists())
        blocked.assignment.assigned_to = self.admin
        blocked.assignment.save()
        set_stage_dates(blocked.pk, self.admin, version_of(self.text), started_at=self.today,
                        ended_at=self.today, finish=True)
        run.refresh_from_db()
        self.assertIsNotNone(run.completed_at)
        self.assertTrue(S.objects.current_cycle().filter(text=self.text, stage_type='ready').exists())
