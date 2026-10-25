"""Redakcja i weryfikacje: przejmowanie tekstu, przekazania do weryfikacji, odmowy przejść przy niespójnym stanie
(bez zmian w danych), uprawnienia i daty rozpoczęcia."""
from datetime import datetime, timedelta

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import SimpleTestCase, TestCase, tag
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import text_detail_context
from core.services.texts import start_assigned_stage
from fantazmaty.testing.workflow import WorkflowTestDataMixin, start_reserved_first_verification
from people.models import Person
from texts.models import Text
from workflow.admin_add_stage import add_missing_stage
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
    send_text_to_author,
    send_to_first_verification,
    send_to_second_verification,
    user_can_complete_stage,
    user_is_assigned_editor,
    validate_assignment_start_date,
)

from .support import importing

S = WorkflowStage
A = WorkflowRoleAssignment
StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


class AssignmentStartDateTests(SimpleTestCase):
    """Dopuszczalna data rozpoczęcia przejmowanej pracy: od dziś do 14 dni naprzód."""

    def test_assignment_start_date_accepts_supported_boundaries(self):
        today = timezone.localdate()

        self.assertEqual(validate_assignment_start_date(today), today)
        self.assertEqual(
            validate_assignment_start_date(today + timedelta(days=14)),
            today + timedelta(days=14),
        )

    def test_assignment_start_date_rejects_out_of_range_dates(self):
        today = timezone.localdate()

        for value in (
            today - timedelta(days=1),
            today + timedelta(days=15),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_assignment_start_date(value)

    def test_assignment_start_date_rejects_invalid_types(self):
        for value in (None, "2026-01-01", datetime(2026, 1, 1)):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_assignment_start_date(value)


@tag('slow')
class EditingAndVerificationFlowTests(WorkflowTestDataMixin, TestCase):
    """Pełna ścieżka redakcji i weryfikacji przez usługi procesu."""

    def test_claiming_editing_assigns_editor_and_prepares_verification(self):
        editing = self.begin_editing()

        self.assertEqual(editing.stage_type, StageType.EDITING)
        self.assertEqual(editing.started_at, self.today)
        self.assertFalse(editing.is_completed)

        self.assertFalse(
            WorkflowStage.objects.filter(pk=self.initial_stage.pk).exists()
        )
        self.assertTrue(
            current_assignment_queryset(self.text).filter(
                role=Role.EDITOR,
                assigned_to=self.editor,
            ).exists()
        )
        self.assertIsNone(
            self.stage(StageType.FIRST_VERIFICATION).started_at
        )

    def test_first_verification_can_wait_for_assignment_after_handover(self):
        text = self.text
        claim_ready_for_editing(text, self.editor)
        pending = send_to_first_verification(text, self.editor)
        self.assertIsNone(pending.started_at)
        self.assertFalse(pending.is_completed)
        self.assertFalse(text.workflow_role_assignments.filter(role="verifier_1").exists())

    def test_same_person_cannot_perform_first_and_second_verification(self):
        self.finish_first_verification()
        resume_editing(self.text, self.editor)
        send_to_second_verification(self.text, self.editor)

        with self.assertRaises(ValidationError):
            claim_stage(
                self.text,
                StageType.SECOND_VERIFICATION,
                self.verifier_1,
            )

        self.assertFalse(
            current_assignment_queryset(self.text).filter(
                role=Role.VERIFIER_2,
            ).exists()
        )
        self.assertIsNone(
            self.stage(StageType.SECOND_VERIFICATION).started_at
        )

    def test_unavailable_stage_cannot_be_created_by_claiming_it(self):
        with self.assertRaises(ValidationError):
            claim_stage(
                self.text,
                StageType.FOURTH_PROOFREADING,
                self.proofreader,
            )

        self.assertFalse(
            current_stage_queryset(self.text).filter(
                stage_type=StageType.FOURTH_PROOFREADING,
            ).exists()
        )
        self.assertFalse(
            current_assignment_queryset(self.text).exists()
        )

    def fresh_text(self):
        """Nowy tekst „Do redakcji” – każdy przypadek tabeli zaczyna od czystego stanu."""
        self.text = Text.objects.create(title="Tekst przypadku", length=1)
        S.objects.create(text=self.text, stage_type=StageType.READY_FOR_EDITING)

    def at_author(self):
        self.finish_first_verification()
        resume_editing(self.text, self.editor)
        return send_text_to_author(self.text, self.editor)

    def test_editorial_transitions_refuse_with_reason_and_change_nothing(self):
        """Każda odmowa przejścia redakcyjnego podaje powód i nie zmienia etapów ani przydziałów."""
        def dateless_author():
            S.objects.filter(pk=self.at_author().pk).update(started_at=None)

        def author_and_editing_both_open():
            author = self.at_author()
            resume_editing(self.text, self.editor)
            S.objects.filter(pk=author.pk).update(is_completed=False, ended_at=None)

        def editing_closed_without_verification():
            editing = self.begin_editing()
            self.stage(StageType.FIRST_VERIFICATION).delete()
            S.objects.filter(pk=editing.pk).update(is_completed=True, ended_at=self.today)

        def handed_to_coordinator():
            self.finish_second_verification()
            resume_editing(self.text, self.editor)
            finish_editing_to_coordinator(self.text, self.editor)

        def coordinator_control_with_inactive_editor():
            A.objects.create(text=self.text, role=Role.EDITOR, assigned_to=self.other_editor)
            S.objects.filter(text=self.text).delete()
            Person.objects.filter(user=self.other_editor).update(is_active=False)
            return S.objects.create(text=self.text, stage_type=StageType.COORDINATOR_CONTROL,
                                    started_at=self.today)

        def stale_copy():
            copy = Text.objects.get(pk=self.text.pk)
            Text.objects.filter(pk=self.text.pk).update(current_workflow_cycle=2)
            return copy

        def waiting_verification():
            self.begin_editing()
            claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
            send_to_first_verification(self.text, self.editor)

        cases = (
            ("rezerwacja W1 z datą", self.begin_editing,
             lambda _: claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1,
                                   started_at=self.today),
             "Rezerwacja pierwszej weryfikacji nie ustala daty"),
            ("wznowienie aktywnej redakcji", self.begin_editing,
             lambda _: resume_editing(self.text, self.editor), "Redakcja jest już aktywna."),
            ("wznowienie, gdy W1 czeka na start", waiting_verification,
             lambda _: resume_editing(self.text, self.editor),
             "Najpierw należy zakończyć oczekującą lub aktywną weryfikację."),
            ("wznowienie przed W1", editing_closed_without_verification,
             lambda _: resume_editing(self.text, self.editor),
             "Redakcja nie może jeszcze zostać wznowiona."),
            ("wznowienie po etapie autora bez daty", dateless_author,
             lambda _: resume_editing(self.text, self.editor),
             "Etap pracy autora nie ma daty rozpoczęcia"),
            ("wznowienie po przekazaniu koordynatorowi", handed_to_coordinator,
             lambda _: resume_editing(self.text, self.editor),
             "Tekst przeszedł już do dalszej części procesu."),
            ("do autora przed W1", self.begin_editing,
             lambda _: send_text_to_author(self.text, self.editor),
             "dopiero po zakończeniu pierwszej weryfikacji"),
            ("do autora bez aktywnej redakcji", self.finish_first_verification,
             lambda _: send_text_to_author(self.text, self.editor),
             "Tekst nie znajduje się obecnie u redaktora."),
            ("do autora drugi raz", author_and_editing_both_open,
             lambda _: send_text_to_author(self.text, self.editor), "Tekst znajduje się już u autora."),
            ("zakończenie redakcji ogólnym przyciskiem", self.begin_editing,
             lambda editing: complete_stage(editing, self.editor, self.today),
             "Redakcję zakończ przez przekazanie tekstu"),
            ("zakończenie etapu u autora", self.at_author,
             lambda author: complete_stage(author, self.editor, self.today),
             "Etap u autora zakończ przez wznowienie redakcji."),
            ("kontrola koordynatora bez aktywnego redaktora", coordinator_control_with_inactive_editor,
             lambda control: complete_stage(control, self.superuser, self.today),
             "przypisz aktywnego redaktora z odpowiednią rolą"),
            ("niezapisany tekst", lambda: None,
             lambda _: claim_ready_for_editing(Text(title="Nowy", length=1), self.editor),
             "Tekst musi być zapisany."),
            ("przebieg zmieniony w międzyczasie", stale_copy,
             lambda copy: claim_ready_for_editing(copy, self.editor), "Przebieg workflow zmienił się."),
        )
        def state():
            return (sorted(S.objects.filter(text=self.text).values_list(
                        "pk", "stage_type", "started_at", "ended_at", "is_completed", "is_current")),
                    sorted(A.objects.filter(text=self.text).values_list("pk", "role", "assigned_to")))

        for name, prepare, action, message in cases:
            with self.subTest(name):
                self.fresh_text()
                target = prepare()
                before = state()
                with self.assertRaisesMessage(ValidationError, message):
                    action(target)
                self.assertEqual(state(), before)

    def test_author_round_trip_preserves_previous_editing_iteration(self):
        self.finish_first_verification()
        editing = resume_editing(self.text, self.editor)

        author_stage = send_text_to_author(
            self.text,
            self.editor,
            started_at=self.today,
        )
        editing.refresh_from_db()

        self.assertTrue(editing.is_completed)
        self.assertEqual(author_stage.started_at, self.today)

        resumed = resume_editing(self.text, self.editor)
        author_stage.refresh_from_db()

        self.assertTrue(author_stage.is_completed)
        self.assertGreater(resumed.iteration, editing.iteration)
        self.assertNotEqual(resumed.pk, editing.pk)

    def test_full_workflow_reaches_ready(self):
        self.finish_second_verification()

        resume_editing(self.text, self.editor)
        finish_editing_to_coordinator(self.text, self.editor)

        steps = (
            (StageType.EDITING_CONTROL, self.coordinator),
            (StageType.FIRST_PROOFREADING, self.proofreader),
            (StageType.SECOND_PROOFREADING, self.proofreading_coordinator),
            (StageType.THIRD_VERIFICATION, self.verifier_1),
            (StageType.COORDINATOR_CONTROL, self.coordinator),
        )

        for stage_type, user in steps:
            with self.subTest(stage=stage_type):
                stage = claim_stage(self.text, stage_type, user)
                complete_stage(stage, user, self.today, send_to_proofreading=True if stage_type == StageType.EDITING_CONTROL else None)

        editor_control = self.stage(StageType.EDITOR_CONTROL)

        self.assertIsNone(editor_control.started_at)
        from core.services.texts import start_assigned_stage
        start_assigned_stage(user=self.editor, stage_id=editor_control.pk, started_at=self.today)
        editor_control.refresh_from_db()
        self.assertFalse(
            user_can_complete_stage(editor_control, self.coordinator)
        )

        complete_stage(editor_control, self.editor, self.today)

        for stage_type, user in (
            (StageType.THIRD_PROOFREADING, self.other_proofreader),
            (StageType.FOURTH_PROOFREADING, self.proofreading_coordinator),
            (StageType.STYLING, self.superuser),
        ):
            stage = claim_stage(self.text, stage_type, user)
            complete_stage(stage, user, self.today)

        ready = self.stage(StageType.READY)

        self.assertEqual(ready.started_at, self.today)
        self.assertFalse(
            current_stage_queryset(self.text)
            .filter(is_completed=False)
            .exclude(stage_type=StageType.READY)
            .exists()
        )

    def test_repeating_completion_does_not_create_another_next_stage(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )
        send_to_first_verification(self.text, self.editor)
        verification = start_reserved_first_verification(self.text, self.verifier_1)
        complete_stage(verification, self.verifier_1, self.today)

        count_before = current_stage_queryset(self.text).count()

        with self.assertRaises(ValidationError):
            complete_stage(verification, self.verifier_1, self.today)

        self.assertEqual(
            current_stage_queryset(self.text).count(),
            count_before,
        )

    def test_imported_undated_first_verification_allows_second(self):
        self.begin_editing()
        self.stage("first_verification").delete()
        with importing():
            first = S.objects.create(
                text=self.text,
                stage_type="first_verification",
                is_completed=True,
                imported_completed=True,
                is_current=False,
            )
        self.assertTrue(
            text_detail_context(user=self.editor, text=self.text)["can_send_to_second_verification"]
        )
        next_stage = send_to_second_verification(self.text, self.editor, self.today)
        self.assertEqual(next_stage.stage_type, "second_verification")
        first.refresh_from_db()
        self.assertIsNone(first.ended_at)

    def test_live_first_verification_still_requires_resumption(self):
        self.begin_editing()
        first = self.stage("first_verification")
        S.objects.filter(pk=first.pk).update(
            started_at=self.today, ended_at=self.today, is_completed=True
        )
        self.assertFalse(
            text_detail_context(user=self.editor, text=self.text)["can_send_to_second_verification"]
        )
        with self.assertRaises(ValidationError):
            send_to_second_verification(self.text, self.editor, self.today)


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


class WorkflowPermissionTests(WorkflowTestDataMixin, TestCase):
    """Kto może przejąć, przekazać i rozpocząć pracę."""

    def test_wrong_role_cannot_claim_editing(self):
        with self.assertRaises(PermissionDenied):
            claim_ready_for_editing(self.text, self.proofreader)

        self.assertTrue(
            WorkflowStage.objects.filter(pk=self.initial_stage.pk).exists()
        )

    def test_former_member_cannot_claim_even_with_django_group(self):
        Person.objects.filter(user=self.editor).update(is_active=False)

        self.assertTrue(
            self.editor.groups.filter(name="Redaktor").exists()
        )

        with self.assertRaises(PermissionDenied):
            claim_ready_for_editing(self.text, self.editor)

    def test_inactive_account_cannot_claim(self):
        self.editor.is_active = False
        self.editor.save(update_fields=["is_active"])

        with self.assertRaises(PermissionDenied):
            claim_ready_for_editing(self.text, self.editor)

    def test_unassigned_editor_cannot_handover_another_persons_text(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )

        with self.assertRaises(PermissionDenied):
            send_to_first_verification(self.text, self.other_editor)

    def test_styling_claim_and_stage_completion_rights(self):
        """Stylowanie przejmuje i zamyka tylko superuser; zamknąć można tylko trwający etap z rolą,
        poza redakcją i pracą autora (te mają własne przejścia), i tylko jako aktywne konto."""
        with self.assertRaisesMessage(PermissionDenied, "Stylowanie może przejąć tylko superuser."):
            claim_stage(self.text, StageType.STYLING, self.coordinator)
        self.assertFalse(current_assignment_queryset(self.text).exists())

        verifier = A.objects.create(text=self.text, role=Role.VERIFIER_1, assigned_to=self.verifier_1)
        inactive = type(self.verifier_1).objects.get(pk=self.verifier_1.pk)
        inactive.is_active = False
        for kind, assignment, user, allowed in (
            (StageType.STYLING, None, self.superuser, True),
            (StageType.STYLING, None, self.coordinator, False),
            (StageType.FIRST_VERIFICATION, verifier, self.verifier_1, True),
            (StageType.FIRST_VERIFICATION, verifier, inactive, False),
            (StageType.EDITING, None, self.superuser, False),
            (StageType.AUTHOR_EDITING, None, self.superuser, False),
            (StageType.READY, None, self.superuser, False),
        ):
            with self.subTest(kind=kind, user=user.username, active=user.is_active):
                S.objects.filter(text=self.text).delete()
                stage = S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                                         started_at=self.today)
                self.assertIs(user_can_complete_stage(stage, user), allowed)

    def test_coordinator_cannot_restart_workflow(self):
        with self.assertRaises(PermissionDenied):
            repeat_stages(
                self.text,
                [StageType.FIRST_PROOFREADING],
                self.coordinator,
            )

    def test_existing_assignment_can_start_without_current_role(self):
        """Regresja: osoba z istniejącym przydziałem nie mogła rozpocząć etapu po utracie roli."""
        text = Text.objects.create(title="Korekta", length=1)
        assignment = A.objects.create(text=text, role="proofreader_1", assigned_to=self.proofreader)
        stage = S.objects.create(text=text, stage_type="first_proofreading", assignment=assignment)
        self.proofreader.person_profile.roles.clear()
        self.proofreader.groups.clear()
        start_assigned_stage(user=self.proofreader, stage_id=stage.pk, started_at=self.today)
        stage.refresh_from_db()
        self.assertEqual(stage.started_at, self.today)


class FirstVerificationStartTests(WorkflowTestDataMixin, TestCase):
    """Pierwsza weryfikacja nie może ruszyć, póki trwa praca redakcyjna."""

    def pending_first(self, editorial_type):
        self.initial_stage.is_current = False
        self.initial_stage.save(update_fields=['is_current'])
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        editorial = S.objects.create(text=self.text, stage_type=editorial_type, assignment=editor)
        verifier = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.verifier_1)
        first = S.objects.create(text=self.text, stage_type='first_verification', assignment=verifier)
        return editorial, first

    def test_open_editing_or_author_stage_blocks_first_verification_start(self):
        """W1 nie rozpocznie się, dopóki redakcja lub praca autora jest otwarta; nic się nie zmienia."""
        for kind in ('editing', 'author_editing'):
            with self.subTest(kind=kind), transaction.atomic():
                editorial, first = self.pending_first(kind)
                with self.assertRaisesMessage(ValidationError, 'Tekst nadal znajduje się u redaktora lub autora.'):
                    start_assigned_stage(user=self.verifier_1, stage_id=first.pk, started_at=self.today)
                first.refresh_from_db()
                editorial.refresh_from_db()
                self.assertIsNone(first.started_at)
                self.assertFalse(editorial.is_completed)
                transaction.set_rollback(True)

    def test_completed_editorial_work_allows_first_verification(self):
        editorial, first = self.pending_first('editing')
        editorial.started_at = editorial.ended_at = timezone.localdate()
        editorial.is_completed = True
        editorial.save()
        started = start_assigned_stage(user=self.verifier_1, stage_id=first.pk, started_at=self.today)
        self.assertEqual(started.pk, first.pk)
        self.assertEqual(started.started_at, self.today)

    def test_archived_verification_is_not_a_missing_stage(self):
        """Zakończonej (zarchiwizowanej) W1/W2 nie da się dodać w adminie drugi raz – ani jako historii, ani z datami."""
        for kind, role in (('first_verification', 'verifier_1'), ('second_verification', 'verifier_2')):
            with self.subTest(kind=kind), transaction.atomic():
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
                transaction.set_rollback(True)


class EditingAfterFirstVerificationTests(TestCase):
    """Czy trwająca redakcja jest już przebiegiem po pierwszej weryfikacji (warunek przekazania do drugiej)."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.yesterday = cls.today - timedelta(days=1)

    def editing_with_first(self, **first):
        text = Text.objects.create(title='Tekst', length=100)
        editing = S.objects.create(text=text, stage_type='editing', started_at=self.today)
        if first.pop('pending', False):
            S.objects.create(text=text, stage_type='first_verification')
        elif first:
            with importing():
                S.objects.create(text=text, stage_type='first_verification', is_completed=True,
                                 is_current=False, **first)
        return text, editing

    def test_order_of_editing_and_completed_first_verification(self):
        """Brak W1, oczekująca rezerwacja W1 albo W1 zakończona w dniu rozpoczęcia redakcji – nie; W1 zakończona
        wcześniej albo zaimportowana bez dat – tak. Bez redakcji – nie."""
        for first, expected in (({}, False),
                                ({'pending': True}, False),
                                ({'started_at': self.yesterday, 'ended_at': self.yesterday}, True),
                                # Same day: the W1 record created after editing began does not approve it.
                                ({'started_at': self.today, 'ended_at': self.today}, False),
                                ({'imported_completed': True}, True)):
            with self.subTest(first=first):
                text, editing = self.editing_with_first(**first)
                self.assertIs(editing_follows_first_verification(text, editing), expected)
        self.assertFalse(editing_follows_first_verification(text, None))
