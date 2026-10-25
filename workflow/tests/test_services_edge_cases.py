"""Strażnicy usług procesu tekstu: odmowy przejść przy niespójnym stanie, powtórzenia etapów
i pomijanie czwartej korekty w kolejce powtórzeń."""
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.edit_versions import version_of
from fantazmaty.testing import create_member, create_superuser
from core.services.texts import start_assigned_stage
from fantazmaty.testing.workflow import WorkflowTestDataMixin, start_reserved_first_verification
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
        first = self.stage(StageType.FIRST_VERIFICATION)

        def start():
            return start_assigned_stage(user=self.verifier_1, stage_id=first.pk, started_at=self.today)

        self.assert_refused(start, 'Najpierw przypisz osobę do tego etapu.')
        claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
        first.refresh_from_db()
        self.assertIsNotNone(first.started_at)
        self.assert_refused(start, 'Data rozpoczęcia tego etapu jest już zapisana.')

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


    def test_stage_cannot_end_before_it_started(self):
        """Data zakończenia wcześniejsza niż rozpoczęcie etapu jest odrzucana, etap pozostaje otwarty."""
        self.begin_editing()
        claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
        send_to_first_verification(self.text, self.editor, ended_at=self.today)
        stage = start_reserved_first_verification(self.text, self.verifier_1, started_at=self.today)
        self.assert_refused(lambda: complete_stage(stage, self.verifier_1, self.today - timedelta(days=1)),
                            'Data zakończenia nie może być wcześniejsza niż data rozpoczęcia.')


    def test_stage_completion_checks_withdrawal_once_and_opens_next_stage_in_one_insert(self):
        """Zakończenie etapu pod blokadą tekstu: stan wycofania sprawdzany raz (nie w każdym kroku),
        następny etap powstaje jednym INSERT-em z datą kolejki, a zapytania o bieżące etapy
        i przydziały nie łączą tabeli tekstów."""
        import re
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from core.workflow_events import event_scope

        self.initial_stage.delete()
        assignment = WorkflowRoleAssignment.objects.create(text=self.text, role='proofreader_1',
                                                           assigned_to=self.proofreader)
        stage = WorkflowStage.objects.create(text=self.text, stage_type=StageType.FIRST_PROOFREADING,
                                             assignment=assignment, started_at=self.today)
        # Dziennik zmian (po operacji) zapisuje zewnętrzny zakres – tu liczymy tylko samo przejście.
        with event_scope(self.superuser), CaptureQueriesContext(connection) as queries:
            complete_stage(stage, self.proofreader, self.today)
        sql = [query['sql'] for query in queries.captured_queries]
        self.assertEqual(len([q for q in sql if re.search(r"stage_type[`\"]? = 'withdrawn'", q)]), 1)
        self.assertEqual(len([q for q in sql if q.startswith('INSERT') and 'workflow_workflowstage' in q]), 1)
        self.assertFalse([q for q in sql if q.startswith('UPDATE') and 'queued_at' in q.split(' WHERE ')[0]])
        self.assertFalse([q for q in sql if re.search(r'= \(?[`"]texts_text[`"]\.[`"]current_workflow_cycle', q)])
        following = self.stage(StageType.SECOND_PROOFREADING)
        self.assertEqual(following.queued_at, self.today)


    def test_change_log_entry_reuses_text_read_with_the_final_state(self):
        """Wpis dziennika zmian po operacji bierze tekst z odczytu stanu końcowego – bez ponownego pobrania."""
        import re
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from core.models import WorkflowEvent

        self.initial_stage.delete()
        assignment = WorkflowRoleAssignment.objects.create(text=self.text, role='proofreader_1',
                                                           assigned_to=self.proofreader)
        stage = WorkflowStage.objects.create(text=self.text, stage_type=StageType.FIRST_PROOFREADING,
                                             assignment=assignment, started_at=self.today)
        with CaptureQueriesContext(connection) as queries:
            complete_stage(stage, self.proofreader, self.today)
        text_reads = [query['sql'] for query in queries.captured_queries
                      if re.match(r'SELECT [^(]*? FROM [`"]texts_text[`"] WHERE', query['sql'])]
        # Stan przed operacją, blokada tekstu i stan po operacji.
        self.assertEqual(len(text_reads), 3, text_reads)
        self.assertTrue(WorkflowEvent.objects.filter(text=self.text, previous_status='Pierwsza korekta').exists())


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

