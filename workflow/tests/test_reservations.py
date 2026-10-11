"""Rezerwacje pierwszej weryfikacji i dodawanie brakujących etapów w adminie (także historii bez dat)."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import _annotated_texts, text_detail_context
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Text
from workflow.admin_add_stage import add_missing_stage
from workflow.admin_stage_edit import edit_stage
from workflow.models import WorkflowHandoff, WorkflowRepetition, WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.read_queries import dashboard_querysets
from workflow.reservation_repair import restore_first_verification_reservation
from workflow.services import (
    completed_stage_exists, send_to_first_verification, send_to_second_verification,
    start_first_verification, complete_stage, resume_editing,
)

from .support import EditingInProgressFixtures, add_roles, importing


class FirstVerificationReservationTests(EditingInProgressFixtures):
    """Przywracanie rezerwacji W1 błędnie zaimportowanej jako zakończona (bez dat) i rezerwacje z admina."""

    def false_completion(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.other, is_current=False)
        with importing():
            return S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment,
                is_current=False, is_released=False, imported_completed=True, is_completed=True)

    def test_preview_writes_nothing_and_repair_changes_second_button_back_to_first(self):
        first = self.false_completion()
        before = text_detail_context(user=self.member, text=self.text)
        self.assertFalse(before['can_send_to_first_verification'])
        self.assertTrue(before['can_send_to_second_verification'])
        version = version_of(self.text)
        preview = restore_first_verification_reservation(first.pk)
        self.assertTrue(preview['changed'])
        self.assertFalse(preview['applied'])
        self.assertTrue(preview['before']['is_completed'])
        first.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertFalse(first.is_current)
        self.assertEqual(version_of(self.text), version)
        original_assignment = first.assignment_id
        restore_first_verification_reservation(first.pk, apply=True)
        first.refresh_from_db()
        self.assertFalse(first.is_completed or first.imported_completed)
        self.assertTrue(first.is_current and first.is_released and first.assignment.is_current)
        self.assertEqual(first.assignment_id, original_assignment)
        self.assertEqual(first.assignment.assigned_to, self.other)
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.ended_at)
        self.assertEqual(S.objects.filter(text=self.text).count(), 2)
        self.assertEqual(A.objects.filter(text=self.text).count(), 2)
        after = text_detail_context(user=self.member, text=self.text)
        self.assertTrue(after['can_send_to_first_verification'])
        self.assertFalse(after['can_send_to_second_verification'])
        with self.assertRaises(ValidationError):
            send_to_second_verification(self.text, self.member, self.today)
        with self.assertRaises(ValidationError):
            start_first_verification(self.text, self.other, self.today)
        send_to_first_verification(self.text, self.member, self.today)
        started = start_first_verification(self.text, self.other, self.today)
        self.assertEqual(started.pk, first.pk)
        complete_stage(started, self.other, self.today)
        resume_editing(self.text, self.member, self.today)
        self.assertTrue(text_detail_context(user=self.member, text=self.text)['can_send_to_second_verification'])

    def test_repair_is_idempotent_and_other_dateless_history_stays_completed(self):
        first = self.false_completion()
        other_text = Text.objects.create(title='W1 rzeczywiście ukończona', anthology=self.book, length=100)
        with importing():
            historical = S.objects.create(text=other_text, stage_type='first_verification',
                                           is_completed=True, imported_completed=True, is_current=False)
        restore_first_verification_reservation(first.pk, apply=True)
        version = version_of(self.text)
        report = restore_first_verification_reservation(first.pk, apply=True)
        self.assertFalse(report['changed'])
        self.assertEqual(version_of(self.text), version)
        historical.refresh_from_db()
        self.assertTrue(historical.is_completed)
        self.assertTrue(completed_stage_exists(other_text, 'first_verification'))

    def test_admin_offers_restoring_archived_reservation_and_saves_without_a_start_date(self):
        first = self.false_completion()
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin:texts_text_change', args=[self.text.pk]))
        self.assertContains(response, '?action=restore_reservation')
        response = self.client.post(reverse('admin:workflow_stage_correct', args=[first.pk]),
            {'action': 'restore_reservation', 'version': version_of(self.text), 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_completed)
        self.assertIsNone(first.started_at)

    def test_non_admin_and_stale_form_cannot_restore_reservation(self):
        first = self.false_completion()
        for actor, version, error in ((self.member, version_of(self.text), PermissionDenied),
                                      (self.admin, version_of(self.text) - 1, ValidationError)):
            with self.assertRaises(error):
                edit_stage(first.pk, actor, version, action='restore_reservation')
        first.refresh_from_db()
        self.assertTrue(first.is_completed)

    def test_repair_rejects_dated_work_further_stages_and_previous_cycle(self):
        first = self.false_completion()
        S.objects.filter(pk=first.pk).update(started_at=self.today, ended_at=self.today)
        with self.assertRaises(ValidationError):
            restore_first_verification_reservation(first.pk, apply=True)
        S.objects.filter(pk=first.pk).update(started_at=None, ended_at=None)
        later = S.objects.create(text=self.text, stage_type='second_verification')
        with self.assertRaises(ValidationError):
            restore_first_verification_reservation(first.pk, apply=True)
        later.delete()
        self.text.current_workflow_cycle = 2
        self.text.save()
        with self.assertRaises(ValidationError):
            restore_first_verification_reservation(first.pk, apply=True)
        first.refresh_from_db()
        self.assertTrue(first.is_completed)
        self.assertFalse(first.is_current)

    def test_repair_refuses_ambiguous_history_and_changes_nothing(self):
        def second_execution(first):
            S.objects.create(text=self.text, stage_type='first_verification', iteration=2,
                             is_current=False, is_released=False)

        def other_current_verifier(first):
            A.objects.create(text=self.text, role='verifier_1', assigned_to=self.admin, execution_number=2)

        def shared_assignment(first):
            S.objects.create(text=self.text, stage_type='ready_for_editing', assignment=first.assignment,
                             is_current=False, is_released=False)

        def handoff(first):
            WorkflowHandoff.objects.create(text=self.text, stage=self.editing, previous_assignment=first.assignment,
                                           new_assignment=self.editor, actor=self.admin, reason='Test')

        def repetition(first):
            WorkflowRepetition.objects.create(text=self.text, completed_at=timezone.now(),
                                              previous_assignment_ids=[first.assignment_id])

        cases = (
            (second_execution, 'więcej niż jedno wykonanie'),
            (lambda first: self.close_editing(), 'jednej aktywnej redakcji'),
            (lambda first: A.objects.filter(pk=first.assignment_id).update(assigned_to=None), 'prawidłowo przypisanej'),
            (other_current_verifier, 'inne aktualne przypisanie'),
            (shared_assignment, 'inne wykonania'),
            (handoff, 'historię przekazania'),
            (repetition, 'częścią powtórzenia'),
        )
        for prepare, message in cases:
            with self.subTest(message=message), transaction.atomic():
                savepoint = transaction.savepoint()
                first = self.false_completion()
                prepare(first)
                with self.assertRaisesMessage(ValidationError, message):
                    restore_first_verification_reservation(first.pk, apply=True)
                first.refresh_from_db()
                self.assertTrue(first.is_completed)
                transaction.savepoint_rollback(savepoint)

    def test_admin_adds_first_reservation_during_editing_and_preserves_active_status(self):
        first = self.reserve()
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.ended_at)
        self.assertFalse(first.is_completed or first.imported_completed)
        self.assertEqual(first.assignment.assigned_to, self.other)
        self.editing.refresh_from_db()
        self.assertFalse(self.editing.is_completed)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'editing')
        with self.assertRaises(ValidationError):
            start_first_verification(self.text, self.other, self.today)

    def test_admin_add_form_reuses_pending_first_stage_and_assignment_for_new_person(self):
        first = self.reserve()
        aid = first.assignment_id
        second = self.reserve(performer=self.admin)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.assignment_id, aid)
        self.assertEqual(second.assignment.assigned_to, self.admin)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        self.assertFalse(second.is_completed)

    def test_admin_post_creates_reservation_with_both_date_fields_blank(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('admin:texts_text_add_stage', args=[self.text.pk]),
            {'kind': 'first_verification', 'performer': self.other.pk,
             'started_at': '', 'ended_at': '', 'version': version_of(self.text)})
        self.assertEqual(response.status_code, 302)
        first = S.objects.get(text=self.text, stage_type='first_verification')
        self.assertIsNone(first.started_at)
        self.assertEqual(first.assignment.assigned_to, self.other)

    def test_admin_cannot_start_first_early_or_replace_genuine_dateless_completion(self):
        with self.assertRaises(ValidationError):
            self.reserve(started_at=self.today)
        first = self.false_completion()
        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
            self.reserve()
        first.refresh_from_db()
        self.assertTrue(first.is_completed)

    def test_reservation_needs_single_unstarted_first_verification_during_editing(self):
        pending = S.objects.create(text=self.text, stage_type='first_verification', started_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'W1 jest już rozpoczęta'):
            self.reserve()
        S.objects.filter(pk=pending.pk).update(started_at=None)
        second = S.objects.create(text=self.text, stage_type='second_verification')
        with self.assertRaisesMessage(ValidationError, 'Najpierw zakończ pozostałe otwarte etapy'):
            self.reserve()
        second.delete()
        self.close_editing()
        with self.assertRaisesMessage(ValidationError, 'Ten etap już istnieje'):
            self.reserve()
        pending.refresh_from_db()
        self.assertIsNone(pending.assignment_id)


class AddMissingStageTests(TestCase):
    """Dodawanie brakującego etapu w adminie: oczekujący, zakończony i historyczny (bez dat)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.users = []
        for i in range(2):
            user = create_member('u' + str(i), 'Redaktor')
            add_roles(user, 'Korektor', 'Weryfikator')
            cls.users.append(user)
        cls.text = Text.objects.create(title="Test", length=100)
        cls.today = timezone.localdate()

    def stage(self, kind, role, user, **kw):
        a = A.objects.create(text=self.text, role=role, assigned_to=user)
        return S.objects.create(text=self.text, stage_type=kind, assignment=a, **kw)

    def test_add_missing_pending_with_performer(self):
        s = add_missing_stage(
            self.text.pk, self.admin, version_of(self.text), kind="editing", performer=self.users[0]
        )
        self.assertEqual(s.assignment.assigned_to_id, self.users[0].pk)
        self.assertIsNone(s.started_at)
        self.assertFalse(s.is_completed)
        with self.assertRaises(ValidationError):
            add_missing_stage(
                self.text.pk,
                self.admin,
                version_of(self.text),
                kind="first_proofreading",
                performer=self.users[1],
            )

    def test_add_completed_missing_does_not_change_open_stage(self):
        open_stage = self.stage("third_proofreading", "proofreader_3", self.users[0])
        s = add_missing_stage(
            self.text.pk,
            self.admin,
            version_of(self.text),
            kind="first_proofreading",
            performer=self.users[1],
            started_at=self.today,
            ended_at=self.today,
        )
        self.assertTrue(s.is_completed)
        open_stage.refresh_from_db()
        self.assertTrue(open_stage.is_current)
        self.assertFalse(open_stage.is_completed)
        with self.assertRaises(ValidationError):
            add_missing_stage(
                self.text.pk,
                self.admin,
                version_of(self.text),
                kind="first_proofreading",
                performer=self.users[1],
                started_at=self.today,
                ended_at=self.today,
            )

    def test_stage_permissions_ready_anthology_dates_and_version(self):
        with self.assertRaises(PermissionDenied):
            add_missing_stage(self.text.pk, self.users[0], version_of(self.text), kind="editing")
        with self.assertRaises(ValidationError):
            add_missing_stage(self.text.pk, self.admin, -1, kind="editing")
        with self.assertRaises(ValidationError):
            add_missing_stage(
                self.text.pk,
                self.admin,
                version_of(self.text),
                kind="editing",
                performer=self.users[0],
                started_at=self.today,
                ended_at=self.today - timedelta(days=1),
            )
        self.assertFalse(S.objects.filter(text=self.text).exists())
        book = Anthology.objects.create(title="Published", status="ready")
        Text.objects.filter(pk=self.text.pk).update(anthology=book)
        with self.assertRaises(ValidationError):
            add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind="editing")

    def test_unsuitable_person_is_validation_error(self):
        with self.assertRaises(ValidationError):
            add_missing_stage(self.text.pk, self.admin, version_of(self.text),
                              kind="second_proofreading", performer=self.users[1])
        self.assertFalse(S.objects.filter(text=self.text).exists())

    def test_history_without_dates_inactive_performer(self):
        user = self.users[0]
        S.objects.create(text=self.text, stage_type="third_verification")
        user.is_active = False
        user.save()
        s = add_missing_stage(
            self.text.pk, self.admin, version_of(self.text),
            kind="editing", performer=user, historical=True,
        )
        self.assertTrue(s.is_completed)
        self.assertTrue(s.imported_completed)
        self.assertIsNone(s.started_at)
        self.assertIsNone(s.ended_at)
        self.assertIsNone(s.assignment.assigned_at)
        active, reserved = dashboard_querysets(user, self.today)
        self.assertFalse(active.filter(pk=s.pk).exists())
        self.assertFalse(reserved.filter(text=self.text).exists())

    def test_live_history_checkbox_required(self):
        with self.assertRaises(ValidationError):
            add_missing_stage(self.text.pk, self.admin, version_of(self.text),
                              kind="editing", performer=self.users[0], ended_at=self.today)

    def test_history_does_not_erase_real_assignment_date(self):
        S.objects.create(text=self.text, stage_type="third_verification")
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.users[0])
        known_date = assignment.assigned_at
        stage = add_missing_stage(self.text.pk, self.admin, version_of(self.text),
                                  kind="editing", performer=self.users[0], historical=True)
        self.assertEqual(stage.assignment_id, assignment.pk)
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_at, known_date)

    def test_incomplete_requests_are_refused_before_any_change(self):
        version = version_of(self.text)
        for kwargs, message in (({'kind': 'ready'}, 'Wybierz etap pracy'),
                                ({'kind': 'editing', 'historical': 'tak', 'performer': self.users[0]}, 'Nieprawidłowy tryb'),
                                ({'kind': 'editing', 'historical': True}, 'wymaga wykonawcy'),
                                ({'kind': 'editing', 'started_at': self.today}, 'Rozpoczęty etap wymaga wykonawcy')):
            with self.subTest(kwargs=kwargs), self.assertRaisesMessage(ValidationError, message):
                add_missing_stage(self.text.pk, self.admin, version, **kwargs)
        WorkflowRepetition.objects.create(text=self.text)
        with self.assertRaisesMessage(ValidationError, 'kolejkę powtórzeń'):
            add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind='editing')
        self.assertFalse(S.objects.filter(text=self.text).exists())

    def test_history_cannot_give_first_proofreading_to_current_later_proofreader(self):
        self.stage('second_proofreading', 'proofreader_2', self.users[0])
        with self.assertRaisesMessage(ValidationError, 'Pierwszą korektę musi wykonać inna osoba'):
            add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind='first_proofreading',
                              performer=self.users[0], historical=True)
        stage = add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind='first_proofreading',
                                  performer=self.users[1], historical=True)
        self.assertTrue(stage.is_completed)
