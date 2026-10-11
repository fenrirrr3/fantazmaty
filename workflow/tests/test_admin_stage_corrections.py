"""Poprawki etapów i rekordów w adminie: cofnięcie zakończenia, usuwanie etapu, ochrona tekstów i przypisań z historią."""
from datetime import timedelta

from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import _annotated_texts, text_detail_context
from fantazmaty.testing import create_member, create_superuser, form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from texts.models import Anthology, Review, Text
from workflow.admin_assignment_edit import correct_assignment
from workflow.admin_stage_edit import edit_stage
from workflow.import_context import importing_completed
from workflow.handoffs import handoff_stage
from workflow.models import WorkflowRepetition, WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.read_queries import annotate_my_work
from workflow.repetitions import repeat_stages
from workflow.services import claim_stage, complete_stage
from workflow.state import current_stage

from .support import importing


class ReopenStageTests(TestCase):
    """Cofnięcie zakończenia etapu przez superusera: ta sama osoba, wykonanie i data rozpoczęcia."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Korektor')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.assignment = A.objects.create(text=cls.text, role='proofreader_1', assigned_to=cls.member)
        cls.stage = S.objects.create(text=cls.text, assignment=cls.assignment, stage_type='first_proofreading',
                                     started_at=cls.today - timedelta(days=10), ended_at=cls.today, is_completed=True)

    def setUp(self):
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
        with importing():
            self.stage.imported_completed = True
            self.stage.save()
        start = self.stage.started_at
        self.reopen()
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.imported_completed)
        self.assertFalse(importing_completed.get())
        self.assertEqual(self.stage.started_at, start)
        self.stage.full_clean()

    def test_imported_stage_without_start_shows_form_error_without_guessing_date(self):
        with importing():
            self.stage.imported_completed = True
            self.stage.started_at = None
            self.stage.ended_at = None
            self.stage.save()
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

    def test_only_genuinely_performed_current_work_can_be_reopened(self):
        def canceled_queue():
            run = WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now())
            S.objects.filter(pk=self.stage.pk).update(repetition=run)

        def other_open_queue():
            WorkflowRepetition.objects.create(text=self.text)

        cases = (
            (canceled_queue, 'odwołanej kolejki'),
            (lambda: S.objects.filter(pk=self.stage.pk).update(is_skipped=True, stage_type='fourth_proofreading',
                                                               assignment=None, started_at=None,
                                                               ended_at=None), 'pominięty'),
            (lambda: S.objects.filter(pk=self.stage.pk).update(started_at=self.today + timedelta(days=1),
                                                               ended_at=self.today + timedelta(days=1)),
             'przyszłej daty rozpoczęcia'),
            (lambda: A.objects.filter(pk=self.assignment.pk).update(is_current=False), 'zastąpionego wykonawcę'),
            (other_open_queue, 'kolejkę powtórzeń'),
        )
        for prepare, message in cases:
            with self.subTest(message=message), transaction.atomic():
                savepoint = transaction.savepoint()
                prepare()
                with self.assertRaisesMessage(ValidationError, message):
                    self.reopen()
                self.assertTrue(S.objects.get(pk=self.stage.pk).is_completed)
                transaction.savepoint_rollback(savepoint)

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


class StageDeletionTests(TestCase):
    """Usunięcie etapu w adminie: bez zastępstwa bieżącym staje się ostatni zakończony etap."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('reader', 'Recenzent')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.today = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.admin)

    def completed(self, kind='editing', **kwargs):
        return S.objects.create(text=self.text, stage_type=kind, is_completed=True,
                                started_at=self.today, ended_at=self.today, **kwargs)

    def test_delete_without_replacement_keeps_completed_last_stage(self):
        last = self.completed('third_proofreading')
        self.completed('editing')  # PK/date must not override workflow order.
        assignment = A.objects.create(text=self.text, role='proofreader_4', assigned_to=self.member)
        removed = S.objects.create(text=self.text, stage_type='fourth_proofreading', assignment=assignment)
        edit_stage(removed.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(S.objects.filter(pk=removed.pk).exists())
        self.assertFalse(A.objects.filter(pk=assignment.pk).exists())
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, last.pk)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, last.stage_type)
        last.refresh_from_db()
        self.assertTrue(last.is_completed)
        self.assertEqual(last.ended_at, self.today)
        self.assertEqual(S.objects.filter(text=self.text).count(), 2)
        detail = text_detail_context(user=self.admin, text=self.text)
        self.assertEqual(len(detail['visible_stages']), len({row['pk'] for row in detail['visible_stages']}))

    def test_admin_delete_form_accepts_empty_replacement(self):
        self.completed()
        removed = S.objects.create(text=self.text, stage_type='first_proofreading')
        url = reverse('admin:workflow_stage_correct', args=[removed.pk])
        response = self.client.post(url, {'action': 'delete', 'replacement': '',
                                          'version': version_of(self.text), 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(S.objects.filter(pk=removed.pk).exists())

    def test_deleting_last_record_leaves_empty_workflow(self):
        removed = S.objects.create(text=self.text, stage_type='ready_for_editing')
        edit_stage(removed.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(self.text.workflow_stages.exists())
        self.assertIsNone(_annotated_texts().get(pk=self.text.pk).current_stage_type)

    def test_explicit_replacement_still_opens_chosen_stage(self):
        self.completed()
        removed = S.objects.create(text=self.text, stage_type='first_proofreading')
        edit_stage(removed.pk, self.admin, version_of(self.text), action='delete', replacement='second_proofreading')
        stage = current_stage(list(self.text.workflow_stages.all()))
        self.assertEqual(stage.stage_type, 'second_proofreading')
        self.assertFalse(stage.is_completed)
        self.assertIsNone(stage.started_at)

    def test_stale_form_and_non_superuser_cannot_delete(self):
        removed = S.objects.create(text=self.text, stage_type='ready_for_editing')
        old = version_of(self.text)
        self.completed()
        with self.assertRaises(ValidationError):
            edit_stage(removed.pk, self.admin, old, action='delete')
        with self.assertRaises(PermissionDenied):
            edit_stage(removed.pk, self.member, version_of(self.text), action='delete')
        self.assertTrue(S.objects.filter(pk=removed.pk).exists())

    def test_closed_anthology_still_blocks_deletion(self):
        removed = S.objects.create(text=self.text, stage_type='ready')
        self.book.status = Anthology.Status.READY
        self.book.save()
        with self.assertRaises(ValidationError):
            edit_stage(removed.pk, self.admin, version_of(self.text), action='delete')
        self.assertTrue(S.objects.filter(pk=removed.pk).exists())

    def test_current_stage_ignores_archived_and_unreleased_work(self):
        last = self.completed()
        self.completed('third_proofreading', is_current=False)
        S.objects.create(text=self.text, stage_type='fourth_proofreading', is_released=False)
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, last.pk)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'editing')



class StageCorrectionFormTests(TestCase):
    """Formularz „Popraw wykonanie etapu”: zmiana wykonawcy w miejscu, ochrona historii powtórzeń i przekazań."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Redaktor')
        cls.other = create_member('other', 'Redaktor')
        cls.today = timezone.localdate()

    def setUp(self):
        self.text = Text.objects.create(title='Tekst', length=100)
        self.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage = S.objects.create(text=self.text, stage_type='editing', assignment=self.assignment,
                                      started_at=self.today)
        self.url = reverse('admin:workflow_stage_correct', args=[self.stage.pk])
        self.client.force_login(self.admin)

    def test_performer_correction_keeps_assignment_and_requires_a_person(self):
        with self.assertRaisesMessage(ValidationError, 'Wybierz wykonawcę'):
            edit_stage(self.stage.pk, self.admin, version_of(self.text), action='performer')
        edit_stage(self.stage.pk, self.admin, version_of(self.text), action='performer', performer=self.other)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.assignment_id, self.assignment.pk)
        self.assertEqual(self.stage.assignment.assigned_to, self.other)
        self.assertEqual(self.stage.started_at, self.today)

    def test_open_repetition_queue_blocks_every_correction(self):
        S.objects.create(text=self.text, stage_type='ready')
        S.objects.filter(pk=self.stage.pk).update(ended_at=self.today, is_completed=True)
        run = repeat_stages(self.text, ['editing'], self.admin)
        queued = run.stages.get()
        for action in ('performer', 'reopen', 'delete', 'restore_reservation'):
            with self.subTest(action=action), self.assertRaisesMessage(ValidationError, 'kolejkę powtórzeń'):
                edit_stage(queued.pk, self.admin, version_of(self.text), action=action, performer=self.other)

    def test_snapshot_stage_and_handed_off_assignment_survive_deletion(self):
        run = WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now(),
                                                previous_stage_ids=[self.stage.pk])
        with self.assertRaisesMessage(ValidationError, 'historii powtórzenia'):
            edit_stage(self.stage.pk, self.admin, version_of(self.text), action='delete')
        run.delete()
        # A stage with a recorded handoff is protected by the database and the form says so.
        handoff_stage(self.text, self.admin, stage_id=self.stage.pk, assigned_to_id=self.other.pk,
                      expected_assignment_id=self.assignment.pk)
        response = self.client.post(self.url, {'action': 'delete', 'version': version_of(self.text),
                                               'confirm': 'on'})
        self.assertContains(response, 'Etap ma powiązane przekazania pracy')
        self.assertTrue(S.objects.filter(pk=self.stage.pk).exists())
        # Deleting the last stage of a handoff participant keeps that assignment.
        control = S.objects.create(text=self.text, stage_type='editing_control', is_current=False,
                                   assignment=self.assignment, is_released=False)
        edit_stage(control.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(S.objects.filter(pk=control.pk).exists())
        self.assertTrue(A.objects.filter(pk=self.assignment.pk).exists())

    def test_form_defaults_to_performer_and_shows_service_errors(self):
        response = self.client.get(self.url, {'action': 'drop_table'})
        self.assertEqual(response.context['form'].initial['action'], 'performer')
        self.assertEqual(response.context['form'].initial['performer'], self.member.pk)
        response = self.client.post(self.url, {'action': 'reopen', 'version': version_of(self.text),
                                               'confirm': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Można cofnąć zakończenie tylko aktualnego')
        response = self.client.post(self.url, {'action': 'performer', 'performer': self.other.pk,
                                               'version': version_of(self.text), 'confirm': 'on'})
        self.assertRedirects(response, reverse('admin:texts_text_change', args=[self.text.pk]),
                             fetch_redirect_response=False)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to, self.other)

class AdminRecordProtectionTests(StatusAssignmentFixtures):
    """Admin nie usuwa tekstów z zapisaną pracą, nie czyści przypisań z historią i nie zmienia numeru przebiegu."""

    def test_clearing_an_assignment_with_finished_work_is_refused(self):
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='editing', assignment=assignment, is_completed=True,
                         started_at=self.today, ended_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'zakończone etapy'):
            correct_assignment(assignment.pk, self.admin, version_of(self.text), action='clear')
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_to, self.member)

    def test_text_with_recorded_work_cannot_be_deleted_in_admin(self):
        from texts.admin import TextAdmin
        request = RequestFactory().get('/')
        request.user = self.admin
        model_admin = TextAdmin(Text, site)
        self.assertTrue(model_admin.has_delete_permission(request, self.text))
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today)
        self.assertFalse(model_admin.has_delete_permission(request, self.text))
        self.assertNotIn('delete_selected', model_admin.get_actions(request))

    def test_deleting_text_detaches_its_source_review(self):
        from texts.admin import TextAdmin
        review = Review.objects.create(title='Źródło', length=100, anthology=self.book, status='accepted',
                                       author_first_name='A', author_last_name='B', email='a@example.test',
                                       copied_text=self.text)
        request = RequestFactory().post('/')
        request.user = self.admin
        TextAdmin(Text, site).delete_model(request, self.text)
        review.refresh_from_db()
        self.assertTrue(review.publication_detached)
        self.assertIsNone(review.copied_text_id)

    def test_cycle_cannot_be_changed_in_admin_forms(self):
        """Numer przebiegu jest w adminie tylko do odczytu – formularz nie może go zmienić."""
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=assignment, started_at=self.today)
        self.client.force_login(self.admin)
        for model, obj in (('workflowroleassignment', assignment), ('workflowstage', stage)):
            url = reverse(f'admin:workflow_{model}_change', args=[obj.pk])
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'name="workflow_cycle"')
            data = {'workflow_cycle': 7, 'notes': 'Uwaga', '_edit_version': form_value(page)}
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 302)
            obj.refresh_from_db()
            self.assertEqual(obj.workflow_cycle, 1)
        assignment.refresh_from_db()
        self.assertEqual(assignment.notes, 'Uwaga')
