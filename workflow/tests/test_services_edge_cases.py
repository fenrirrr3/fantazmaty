"""Strażnicy usług procesu tekstu: odmowy przejść przy niespójnym stanie, powtórzenia etapów
i pomijanie czwartej korekty w kolejce powtórzeń."""
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.edit_versions import version_of
from fantazmaty.testing import create_member, create_superuser
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from texts.models import Anthology, Text
from workflow.admin_status import set_admin_status
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.repetitions import repeat_stages
from workflow.services import (
    claim_ready_for_editing,
    claim_stage,
    complete_stage,
    completed_stage_exists,
    current_assignment_queryset,
    current_stage_queryset,
    editing_follows_first_verification,
    finish_editing_to_coordinator,
    resume_editing,
    send_to_first_verification,
    send_to_second_verification,
    skip_fourth_proofreading,
    start_first_verification,
    user_can_complete_stage,
    user_is_assigned_editor,
)


S = WorkflowStage
A = WorkflowRoleAssignment
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


class TransitionGuardTests(WorkflowTestDataMixin, TestCase):
    """Przejścia redakcji odmawiają, gdy tekst nie jest w oczekiwanym stanie, i niczego nie zmieniają."""

    def snapshot(self):
        return list(S.objects.filter(text=self.text).order_by('pk').values_list(
            'pk', 'stage_type', 'started_at', 'ended_at', 'is_completed', 'is_current', 'queued_at'))

    def assert_refused(self, operation, message, error=ValidationError):
        before = self.snapshot()
        with self.assertRaisesMessage(error, message):
            operation()
        self.assertEqual(self.snapshot(), before)

    def test_unsaved_records_are_rejected_without_queries_on_them(self):
        unsaved = S(text=self.text, stage_type=StageType.FIRST_PROOFREADING)
        self.assert_refused(lambda: complete_stage(unsaved, self.superuser, self.today),
                            'Etap musi być zapisany')
        self.assertFalse(user_can_complete_stage(S(stage_type=StageType.FIRST_PROOFREADING), self.superuser))

    def test_inactive_or_anonymous_user_is_never_the_assigned_editor(self):
        self.begin_editing()
        self.assertTrue(user_is_assigned_editor(self.text, self.editor))
        self.editor.is_active = False
        self.assertFalse(user_is_assigned_editor(self.text, self.editor))
        self.assertFalse(user_is_assigned_editor(self.text, AnonymousUser()))

    def test_completed_check_for_other_stage_uses_only_current_path(self):
        """Poza weryfikacjami liczy się tylko bieżąca ścieżka – wycofany z niej etap nie jest „zakończony”."""
        control = S.objects.create(text=self.text, stage_type=StageType.EDITING_CONTROL,
                                   started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertTrue(completed_stage_exists(self.text, StageType.EDITING_CONTROL))
        S.objects.filter(pk=control.pk).update(is_current=False)
        self.assertFalse(completed_stage_exists(self.text, StageType.EDITING_CONTROL))

    def test_editing_entry_refuses_when_already_taken_or_reserved(self):
        cases = {
            'etap „Do redakcji” zniknął po przejęciu': (
                self.begin_editing, 'nie znajduje się na etapie'),
            'redaktor ma już rezerwację tekstu': (
                lambda: A.objects.create(text=self.text, role=Role.EDITOR, assigned_to=self.editor),
                'przypisano już redaktora'),
        }
        for label, (prepare, message) in cases.items():
            with self.subTest(label):
                prepare()
                self.assert_refused(lambda: claim_ready_for_editing(self.text, self.other_editor), message)
                S.objects.filter(text=self.text).delete()
                A.objects.filter(text=self.text).delete()
                S.objects.create(text=self.text, stage_type=StageType.READY_FOR_EDITING)

    def test_handoffs_without_active_editing_are_refused(self):
        self.assert_refused(lambda: send_to_first_verification(self.text, self.coordinator),
                            'Nie ma aktywnej redakcji')
        self.finish_first_verification()
        self.assert_refused(lambda: send_to_second_verification(self.text, self.editor),
                            'musi znajdować się u redaktora')
        self.finish_second_verification_from_here()
        self.assert_refused(lambda: finish_editing_to_coordinator(self.text, self.editor),
                            'Nie ma aktywnej redakcji do zakończenia')

    def finish_second_verification_from_here(self):
        resume_editing(self.text, self.editor, started_at=self.today)
        send_to_second_verification(self.text, self.editor, started_at=self.today)
        stage = claim_stage(self.text, StageType.SECOND_VERIFICATION, self.verifier_2)
        complete_stage(stage, self.verifier_2, self.today)

    def test_first_verification_start_needs_assignee_and_a_waiting_stage(self):
        self.begin_editing()
        send_to_first_verification(self.text, self.editor)
        self.assert_refused(lambda: start_first_verification(self.text, self.verifier_1),
                            'nie przypisano osoby')
        claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
        self.stage(StageType.FIRST_VERIFICATION).refresh_from_db()
        self.assertIsNotNone(self.stage(StageType.FIRST_VERIFICATION).started_at)
        self.assert_refused(lambda: start_first_verification(self.text, self.verifier_1),
                            'nie oczekuje na rozpoczęcie')

    def test_started_first_verification_blocks_second_handoff_during_editing(self):
        """Niespójne dane z korekty w adminie: W1 ma datę rozpoczęcia, a redakcja nadal trwa."""
        self.begin_editing()
        S.objects.filter(text=self.text, stage_type=StageType.FIRST_VERIFICATION).update(started_at=self.today)
        self.assert_refused(lambda: send_to_first_verification(self.text, self.editor),
                            'została już rozpoczęta')

    def test_second_verification_already_created_is_not_duplicated(self):
        self.finish_first_verification()
        resume_editing(self.text, self.editor, started_at=self.today)
        S.objects.create(text=self.text, stage_type=StageType.SECOND_VERIFICATION)
        self.assert_refused(lambda: send_to_second_verification(self.text, self.editor),
                            'została już utworzona')

    def test_coordinator_resumes_editing_without_assigned_editor(self):
        self.finish_first_verification()
        A.objects.filter(text=self.text, role=Role.EDITOR).update(assigned_to=None)
        editing = resume_editing(self.text, self.coordinator, started_at=self.today)
        self.assertEqual(editing.stage_type, StageType.EDITING)
        self.assertEqual(editing.started_at, self.today)
        self.assertEqual(editing.iteration, 2)

    def test_open_repetition_blocks_ordinary_editing_handoffs(self):
        self.finish_first_verification()
        repeat_stages(self.text, [StageType.FIRST_VERIFICATION], self.superuser,
                      assignees={Role.VERIFIER_1: self.verifier_2})
        self.assert_refused(lambda: resume_editing(self.text, self.editor), 'W powtórzeniu użyj przycisku')


class EditingAfterFirstVerificationTests(WorkflowTestDataMixin, TestCase):
    """Czy trwająca redakcja nastąpiła po zakończonej pierwszej weryfikacji."""

    def test_missing_or_pending_verification_does_not_approve_editing(self):
        editing = self.begin_editing()
        self.assertFalse(editing_follows_first_verification(self.text, None))
        # Rezerwacja W1 nadal czeka, więc redakcja nie jest drugą rundą.
        self.assertFalse(editing_follows_first_verification(self.text, editing))


class CycleEntryClaimTests(TestCase):
    """Tekst ustawiony w adminie na „Redakcja” – przejęcie pierwszego etapu przygotowuje rezerwację W1."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('cykl-admin')
        cls.editor = create_member('cykl-redaktor', 'Redaktor')

    def test_claiming_lone_editing_stage_prepares_first_verification_once(self):
        """Nowa rezerwacja W1 powstaje tylko wtedy, gdy pierwsza weryfikacja nie została już wykonana."""
        book = Anthology.objects.create(title='Antologia')
        for title, verified in (('Wejście w proces', False), ('Powrót po weryfikacji', True)):
            with self.subTest(title):
                text = Text.objects.create(title=title, length=100, anthology=book)
                S.objects.create(text=text, stage_type=StageType.READY_FOR_EDITING)
                if verified:
                    S.objects.create(text=text, stage_type=StageType.FIRST_VERIFICATION, is_completed=True,
                                     started_at=timezone.localdate(), ended_at=timezone.localdate())
                set_admin_status(text.pk, StageType.EDITING, self.admin, version_of(text))
                editing = claim_stage(text, StageType.EDITING, self.editor)
                editing.refresh_from_db()
                self.assertEqual(editing.started_at, timezone.localdate())
                self.assertTrue(current_assignment_queryset(text).filter(
                    role=Role.EDITOR, assigned_to=self.editor).exists())
                pending = current_stage_queryset(text).filter(stage_type=StageType.FIRST_VERIFICATION)
                self.assertEqual(pending.exists(), not verified)
                self.assertFalse(pending.filter(started_at__isnull=False).exists())


class RepetitionQueueTests(WorkflowTestDataMixin, TestCase):
    """Kolejka powtórzeń czwartej korekty i stylowania."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.next_coordinator = create_member('kolejny_koordynator_korekty', 'Koordynator korekty')
        cls.next_admin = create_superuser('kolejny_superuser')

    def setUp(self):
        super().setUp()
        S.objects.filter(text=self.text).delete()
        previous = self.today
        self.fourth = A.objects.create(text=self.text, role=Role.PROOFREADER_4,
                                       assigned_to=self.proofreading_coordinator)
        self.styling = A.objects.create(text=self.text, role=Role.STYLING, assigned_to=self.superuser)
        for kind, assignment in ((StageType.FOURTH_PROOFREADING, self.fourth),
                                 (StageType.STYLING, self.styling), (StageType.READY, None)):
            S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                             started_at=previous, ended_at=previous, is_completed=True)

    def repeat(self, assignees=None):
        return repeat_stages(self.text, [StageType.FOURTH_PROOFREADING, StageType.STYLING],
                             self.superuser, assignees=assignees)

    def test_ineligible_repeat_performer_is_reported_as_form_error(self):
        with self.assertRaisesMessage(ValidationError, 'Stylowanie można przypisać tylko superuserowi'):
            self.repeat({Role.PROOFREADER_4: self.next_coordinator, Role.STYLING: self.editor})
        self.assertFalse(self.text.repetitions.exists())

    def test_skipping_fourth_in_queue_releases_styling(self):
        run = self.repeat()
        fourth = run.stages.get(stage_type=StageType.FOURTH_PROOFREADING)
        styling = run.stages.get(stage_type=StageType.STYLING)
        self.assertFalse(styling.is_released)
        skip_fourth_proofreading(fourth, self.superuser)
        fourth.refresh_from_db()
        styling.refresh_from_db()
        self.assertTrue(fourth.is_skipped)
        self.assertTrue(fourth.is_completed)
        self.assertTrue(styling.is_released)
        run.refresh_from_db()
        self.assertIsNone(run.completed_at)

    def test_assigned_repeat_performer_starts_once(self):
        """Osoba wskazana przy powtórzeniu rozpoczyna etap; drugie rozpoczęcie jest odrzucane."""
        self.repeat({Role.PROOFREADER_4: self.next_coordinator, Role.STYLING: self.next_admin})
        stage = claim_stage(self.text, StageType.FOURTH_PROOFREADING, self.next_coordinator)
        self.assertEqual(stage.started_at, self.today)
        self.assertEqual(stage.assignment.assigned_to, self.next_coordinator)
        with self.assertRaisesMessage(ValidationError, 'nie oczekuje na rozpoczęcie'):
            claim_stage(self.text, StageType.FOURTH_PROOFREADING, self.next_coordinator)


class NumberingTests(TestCase):
    """Wspólne liczniki z ``workflow.numbering``: zakres tekst + przebieg (+ rola / rodzaj etapu)."""

    @classmethod
    def setUpTestData(cls):
        cls.text = Text.objects.create(title='Numerowany', length=100)
        cls.other_text = Text.objects.create(title='Inny', length=100)

    def test_next_numbers_count_only_matching_text_cycle_and_kind(self):
        from workflow.numbering import next_assignment_execution, next_stage_execution, next_stage_iteration
        cases = [
            ('brak rekordów', [], [], 1, 1, 1),
            ('jeden rekord', [dict(execution_number=1)], [dict(iteration=1, execution_number=1)], 2, 2, 2),
            ('nieaktualne też liczą się',
             [dict(execution_number=3, is_current=False)], [dict(iteration=3, execution_number=2, is_current=False)],
             4, 4, 3),
            ('inny przebieg nie liczy się',
             [dict(execution_number=2, workflow_cycle=2)], [dict(iteration=2, execution_number=2, workflow_cycle=2)],
             1, 1, 1),
            ('inna rola / rodzaj etapu nie liczy się',
             [dict(execution_number=2, role=Role.VERIFIER_2)],
             [dict(iteration=2, execution_number=2, stage_type=StageType.SECOND_VERIFICATION)],
             1, 1, 1),
            ('inny tekst nie liczy się',
             [dict(execution_number=2, text=self.other_text)],
             [dict(iteration=2, execution_number=2, text=self.other_text)],
             1, 1, 1),
        ]
        for name, assignments, stages, assignment_next, iteration_next, execution_next in cases:
            with self.subTest(case=name):
                for fields in assignments:
                    A.objects.create(**{'text': self.text, 'role': Role.VERIFIER_1, **fields})
                for fields in stages:
                    S.objects.create(**{'text': self.text, 'stage_type': StageType.FIRST_VERIFICATION, **fields})
                self.assertEqual(next_assignment_execution(self.text, Role.VERIFIER_1), assignment_next)
                self.assertEqual(next_stage_iteration(self.text, StageType.FIRST_VERIFICATION), iteration_next)
                self.assertEqual(next_stage_execution(self.text, StageType.FIRST_VERIFICATION), execution_next)
                S.objects.all().delete()
                A.objects.all().delete()

    def test_explicit_cycle_and_stage_execution_from_assignment(self):
        """Jawny przebieg liczy w nim, a numer etapu wynika z przydziału (brak przydziału → 1)."""
        from workflow.numbering import next_assignment_execution, next_stage_iteration, stage_execution_for
        A.objects.create(text=self.text, role=Role.EDITOR, workflow_cycle=2, execution_number=2, is_current=False)
        S.objects.create(text=self.text, stage_type=StageType.EDITING, workflow_cycle=2, iteration=5, is_current=False)
        self.assertEqual(next_assignment_execution(self.text, Role.EDITOR, cycle=2), 3)
        self.assertEqual(next_stage_iteration(self.text, StageType.EDITING, cycle=2), 6)
        self.assertEqual(next_assignment_execution(self.text, Role.EDITOR), 1)
        self.assertEqual(stage_execution_for(None), 1)
        self.assertEqual(stage_execution_for(A(execution_number=4)), 4)


class AlignStageExecutionsCommandTests(TestCase):
    """Komenda ``align_stage_executions``: wyrównuje numer otwartego etapu do przydziału, pomija zakończone,
    nieaktualne, bez przydziału i po przekazaniu; ``--dry-run`` niczego nie zapisuje."""

    @classmethod
    def setUpTestData(cls):
        from workflow.models import WorkflowHandoff
        cls.text = Text.objects.create(title='Rozjechany', length=100)
        cls.assignment = A.objects.create(text=cls.text, role=Role.PROOFREADER_1, execution_number=2)
        make = lambda kind, iteration, **fields: S.objects.create(  # noqa: E731
            text=cls.text, stage_type=kind, iteration=iteration, assignment=cls.assignment, execution_number=1, **fields)
        cls.open = make(StageType.FIRST_PROOFREADING, 1)
        today = timezone.localdate()
        cls.done = make(StageType.FIRST_PROOFREADING, 2, is_completed=True, started_at=today, ended_at=today)
        cls.stale = make(StageType.FIRST_PROOFREADING, 3, is_current=False)
        cls.handed = make(StageType.SECOND_PROOFREADING, 1)
        previous = A.objects.create(text=cls.text, role=Role.PROOFREADER_1, execution_number=1, is_current=False)
        WorkflowHandoff.objects.create(text=cls.text, stage=cls.handed, previous_assignment=previous,
                                       new_assignment=cls.assignment)
        cls.unassigned = S.objects.create(text=cls.text, stage_type=StageType.EDITING, iteration=1, execution_number=1)

    def numbers(self):
        return {stage.pk: S.objects.get(pk=stage.pk).execution_number
                for stage in (self.open, self.done, self.stale, self.handed, self.unassigned)}

    def test_only_open_current_assigned_stages_without_handoff_are_aligned(self):
        from io import StringIO
        from django.core.management import call_command
        before = self.numbers()
        out = StringIO()
        call_command('align_stage_executions', '--dry-run', stdout=out)
        self.assertEqual(self.numbers(), before)
        self.assertIn('Rozjechany: Pierwsza korekta wyk. 1 -> 2', out.getvalue())
        self.assertIn('Do zmiany: 1 (bez zapisu).', out.getvalue())
        out = StringIO()
        call_command('align_stage_executions', stdout=out)
        self.assertIn('Poprawiono: 1.', out.getvalue())
        self.assertEqual(self.numbers(), {**before, self.open.pk: 2})
        out = StringIO()
        call_command('align_stage_executions', stdout=out)
        self.assertIn('Wszystkie otwarte etapy mają numer zgodny z przydziałem.', out.getvalue())

