"""Business rules explicitly agreed after the audit (2026-10-03)."""
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.forms import CompleteStageForm
from core.selectors.reports import workflow_inactivity_context
from core.selectors.texts import text_detail_context, user_workflow_summary, workflow_list_context
from core.services.texts import start_assigned_stage
from texts.models import Anthology, Text
from workflow.admin_performers import correct_stage_performers
from workflow.admin_stage_dates import StageDatesForm, set_stage_dates
from workflow.handoffs import handoff_stage
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowRepetition
from workflow.read_queries import annotate_my_work
from workflow.services import complete_stage, finish_editing_to_coordinator, send_to_first_verification, send_to_second_verification
from workflow.tests import WorkflowTestDataMixin
from workflow.waiting import annotate_inactivity_clocks


class AgreedWorkflowTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        override.enable()
        self.addCleanup(override.disable)
        self.text.anthology = Anthology.objects.create(title="Uzgodnione zasady")
        self.text.save()

    def control(self):
        self.initial_stage.delete()
        editor = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=editor,
            started_at=self.today - timedelta(days=3),
            ended_at=self.today - timedelta(days=2),
            is_completed=True,
        )
        assignment = A.objects.create(
            text=self.text, role="editing_coordinator", assigned_to=self.coordinator
        )
        return S.objects.create(
            text=self.text,
            stage_type="editing_control",
            assignment=assignment,
            started_at=self.today - timedelta(days=1),
        )

    def work(self):
        return annotate_my_work(Text.objects.all(), self.editor, self.today).get(pk=self.text.pk)

    def complete_post(self, stage, decision=None):
        self.client.force_login(self.coordinator)
        page = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
        form = html.fromstring(page.content).xpath(
            "//form[@action=$url]", url=reverse("core:complete_workflow_stage", args=[stage.pk])
        )[0]
        data = {
            node.get("name"): node.get("value", "")
            for node in form.xpath('.//input[@type="hidden"]')
        }
        if decision is not None:
            data["send_to_proofreading"] = decision
        return self.client.post(form.get("action"), data)

    def test_coordinator_must_choose_and_cannot_silently_finish(self):
        stage = self.control()
        for choice in (None, "", "yes", 1):
            with self.subTest(choice=choice), self.assertRaises(ValidationError):
                complete_stage(stage, self.coordinator, self.today, send_to_proofreading=choice)
        stage.refresh_from_db()
        self.assertFalse(stage.is_completed)
        self.assertIsNone(stage.send_to_proofreading)
        self.assertFalse(S.objects.filter(text=self.text, stage_type="first_proofreading").exists())

    def test_http_form_requires_an_explicit_boolean(self):
        stage = self.control()
        for decision in (None, "invalid"):
            self.assertEqual(self.complete_post(stage, decision).status_code, 302)
            stage.refresh_from_db()
            self.assertFalse(stage.is_completed)
        self.assertEqual(self.complete_post(stage, "true").status_code, 302)
        stage.refresh_from_db()
        self.assertTrue(stage.is_completed and stage.send_to_proofreading)

    def test_approval_finishes_editorial_work_before_proofreader_starts(self):
        stage = self.control()
        self.assertTrue(self.work().work_waiting)
        complete_stage(stage, self.coordinator, self.today, send_to_proofreading=True)
        proof = self.stage("first_proofreading")
        self.assertIsNone(proof.started_at)
        self.assertTrue(self.work().work_completed)
        self.assertFalse(self.work().work_waiting)
        self.assertEqual(user_workflow_summary(self.editor)["active_stage_count"], 0)
        row = list(workflow_list_context(user=self.superuser, params={})["stages"])[0]
        self.assertEqual(
            next(cell for cell in row["role_cells"] if cell["role"] == "editor")["entries"][0][
                "state"
            ],
            "Zakończone",
        )

    def test_proofreading_alone_cannot_substitute_coordinator_approval(self):
        self.control()
        S.objects.create(text=self.text, stage_type="first_proofreading")
        self.assertFalse(self.work().work_completed)
        self.assertTrue(self.work().work_waiting)

    def test_rejection_opens_next_editorial_pass_and_can_return_for_approval(self):
        stage = self.control()
        for kind in ("first_verification", "second_verification"):
            S.objects.create(
                text=self.text,
                stage_type=kind,
                started_at=self.today - timedelta(days=2),
                ended_at=self.today - timedelta(days=2),
                is_completed=True,
            )
        complete_stage(stage, self.coordinator, self.today, send_to_proofreading=False)
        stage.refresh_from_db()
        self.assertFalse(stage.send_to_proofreading)
        self.assertTrue(stage.is_completed)
        self.assertFalse(stage.is_current)
        editing = self.stage("editing")
        self.assertEqual(editing.iteration, 2)
        self.assertEqual(editing.assignment.assigned_to_id, self.editor.pk)
        self.assertIsNone(editing.started_at)
        self.assertTrue(text_detail_context(user=self.editor, text=self.text)["can_resume_editing"])
        self.assertTrue(self.work().work_waiting)
        self.assertFalse(S.objects.filter(text=self.text, stage_type="first_proofreading").exists())
        start_assigned_stage(user=self.editor, stage_id=editing.pk, started_at=self.today)
        finish_editing_to_coordinator(self.text, self.editor, self.today)
        again = self.stage("editing_control")
        self.assertEqual(again.iteration, 2)
        start_assigned_stage(user=self.coordinator, stage_id=again.pk, started_at=self.today)
        complete_stage(again, self.coordinator, self.today, send_to_proofreading=True)
        self.assertTrue(self.work().work_completed)

    def test_rejection_without_editor_is_atomic(self):
        stage = self.control()
        A.objects.filter(text=self.text, role="editor").update(assigned_to=None)
        with self.assertRaises(ValidationError):
            complete_stage(stage, self.coordinator, self.today, send_to_proofreading=False)
        stage.refresh_from_db()
        self.assertFalse(stage.is_completed)

    def test_unassigned_user_cannot_make_coordinator_decision(self):
        stage = self.control()
        with self.assertRaises(PermissionDenied):
            complete_stage(stage, self.other_editor, self.today, send_to_proofreading=True)
        stage.refresh_from_db()
        self.assertFalse(stage.is_completed)

    def test_ready_text_rejects_control_transition_without_repair(self):
        stage = self.control()
        S.objects.create(text=self.text, stage_type="ready")
        before = list(S.objects.filter(text=self.text).order_by("pk").values())
        with self.assertRaises(ValidationError):
            complete_stage(stage, self.coordinator, self.today, send_to_proofreading=True)
        self.assertEqual(list(S.objects.filter(text=self.text).order_by("pk").values()), before)

    def test_return_to_editor_retires_unstarted_downstream_reservation(self):
        stage = self.control()
        proof = S.objects.create(text=self.text, stage_type="first_proofreading")
        complete_stage(stage, self.coordinator, self.today, send_to_proofreading=False)
        proof.refresh_from_db()
        self.assertFalse(proof.is_current or proof.is_completed)
        self.assertTrue(text_detail_context(user=self.editor, text=self.text)["can_resume_editing"])

    def test_return_cannot_rewrite_already_started_downstream_work(self):
        stage = self.control()
        proof = S.objects.create(
            text=self.text, stage_type="first_proofreading", started_at=self.today
        )
        with self.assertRaises(ValidationError):
            complete_stage(stage, self.coordinator, self.today, send_to_proofreading=False)
        stage.refresh_from_db()
        proof.refresh_from_db()
        self.assertFalse(stage.is_completed)
        self.assertTrue(proof.is_current)

    def test_repeated_submission_does_not_create_two_continuations(self):
        stage = self.control()
        complete_stage(stage, self.coordinator, self.today, send_to_proofreading=True)
        with self.assertRaises(ValidationError):
            complete_stage(stage, self.coordinator, self.today, send_to_proofreading=True)
        self.assertEqual(
            S.objects.filter(text=self.text, stage_type="first_proofreading").count(), 1
        )

    def test_admin_date_form_requires_choice_only_when_finishing(self):
        stage = self.control()
        data = {
            "version": version_of(self.text),
            "started_at": stage.started_at,
            "ended_at": self.today,
            "finish": "on",
        }
        form = StageDatesForm(data, stage=stage)
        self.assertFalse(form.is_valid())
        self.assertIn("send_to_proofreading", form.errors)
        data["send_to_proofreading"] = "false"
        form = StageDatesForm(data, stage=stage)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIs(form.cleaned_data["send_to_proofreading"], False)
        set_stage_dates(
            stage.pk,
            self.superuser,
            version_of(self.text),
            started_at=stage.started_at,
            ended_at=self.today,
            finish=True,
            send_to_proofreading=False,
        )
        self.assertEqual(self.stage("editing").assignment.assigned_to_id, self.editor.pk)

    def test_other_stage_forms_do_not_require_coordinator_decision(self):
        stage = self.begin_editing()
        form = CompleteStageForm({"ended_at": self.today}, stage=stage)
        self.assertTrue(form.is_valid())
        self.assertNotIn("send_to_proofreading", form.fields)

    def test_editor_returns_only_when_own_control_is_available(self):
        control = self.control()
        complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        proof = self.stage("first_proofreading")
        S.objects.filter(pk=proof.pk).update(
            started_at=self.today, ended_at=self.today, is_completed=True
        )
        coordinator_assignment = A.objects.create(
            text=self.text, role="verification_coordinator", assigned_to=self.coordinator
        )
        later = S.objects.create(
            text=self.text,
            stage_type="coordinator_control",
            assignment=coordinator_assignment,
            started_at=self.today,
        )
        self.assertEqual(user_workflow_summary(self.editor)["active_stage_count"], 0)
        complete_stage(later, self.coordinator, self.today)
        summary = user_workflow_summary(self.editor)
        self.assertEqual(summary["active_stage_count"], 1)
        self.assertEqual(summary["active_stages"][0]["stage_type"], "editor_control")

    def test_imported_undated_first_verification_allows_second(self):
        self.begin_editing()
        self.stage("first_verification").delete()
        token = importing_completed.set(True)
        try:
            first = S.objects.create(
                text=self.text,
                stage_type="first_verification",
                is_completed=True,
                imported_completed=True,
                is_current=False,
            )
        finally:
            importing_completed.reset(token)
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

    def test_ready_text_is_not_repaired_or_reclassified(self):
        stage = self.begin_editing()
        S.objects.filter(pk=stage.pk).update(ended_at=self.today, is_completed=True)
        self.stage("first_verification").delete()
        S.objects.create(text=self.text, stage_type="ready")
        before = list(S.objects.filter(text=self.text).order_by("pk").values())
        self.assertFalse(self.work().work_completed)  # v2 classification retained
        self.assertFalse(self.work().work_active or self.work().work_waiting)
        text_detail_context(user=self.editor, text=self.text)
        workflow_inactivity_context(user=self.superuser, params=QueryDict(""))
        self.assertEqual(list(S.objects.filter(text=self.text).order_by("pk").values()), before)

    def test_coordinator_rejection_inside_repeat_preserves_following_queue(self):
        stage = self.control()
        run = WorkflowRepetition.objects.create(
            text=self.text,
            created_by=self.superuser,
            selected_stages=["editing_control", "first_proofreading"],
        )
        S.objects.filter(pk=stage.pk).update(repetition=run, queue_position=0)
        stage.refresh_from_db()
        proof = S.objects.create(
            text=self.text,
            stage_type="first_proofreading",
            repetition=run,
            queue_position=1,
            is_released=False,
        )
        complete_stage(stage, self.coordinator, self.today, send_to_proofreading=False)
        remaining = list(run.stages.filter(is_completed=False).order_by("queue_position"))
        self.assertEqual(
            [s.stage_type for s in remaining], ["editing", "editing_control", "first_proofreading"]
        )
        self.assertEqual([s.is_released for s in remaining], [True, False, False])
        editing, second_control, _ = remaining
        start_assigned_stage(user=self.editor, stage_id=editing.pk, started_at=self.today)
        complete_stage(editing, self.editor, self.today)
        start_assigned_stage(
            user=self.coordinator, stage_id=second_control.pk, started_at=self.today
        )
        complete_stage(second_control, self.coordinator, self.today, send_to_proofreading=True)
        proof.refresh_from_db()
        self.assertTrue(proof.is_released)
        self.assertEqual(run.stages.filter(stage_type="first_proofreading").count(), 1)

    def clock(self, stage):
        return annotate_inactivity_clocks(S.objects.filter(pk=stage.pk), self.today).get()

    def test_early_reservation_is_zero_before_and_after_actual_handoff(self):
        self.begin_editing()
        first = self.stage("first_verification")
        S.objects.filter(pk=first.pk).update(queued_at=self.today - timedelta(days=40))
        row = self.clock(first)
        self.assertTrue(row.report_blocked)
        self.assertEqual(row.report_waiting_since, self.today)
        send_to_first_verification(self.text, self.editor, self.today)
        row = self.clock(first)
        self.assertFalse(row.report_blocked)
        self.assertEqual(row.report_waiting_since, self.today)
        self.assertFalse(
            workflow_inactivity_context(user=self.superuser, params=QueryDict("mode=waiting"))[
                "rows"
            ]
        )

    def test_legacy_reservation_uses_relevant_completion_not_unrelated_activity(self):
        editing = self.begin_editing()
        first = self.stage("first_verification")
        S.objects.filter(pk=first.pk).update(queued_at=self.today - timedelta(days=40))
        S.objects.filter(pk=editing.pk).update(
            started_at=self.today - timedelta(days=35),
            ended_at=self.today - timedelta(days=20),
            is_completed=True,
        )
        S.objects.create(
            text=self.text,
            stage_type="styling",
            started_at=self.today,
            ended_at=self.today,
            is_completed=True,
        )
        self.assertEqual(self.clock(first).report_waiting_since, self.today - timedelta(days=20))

    def test_unknown_predecessor_date_remains_unknown(self):
        self.initial_stage.delete()
        token = importing_completed.set(True)
        try:
            S.objects.create(
                text=self.text, stage_type="editing", imported_completed=True, is_completed=True
            )
        finally:
            importing_completed.reset(token)
        first = S.objects.create(
            text=self.text,
            stage_type="first_verification",
            queued_at=self.today - timedelta(days=40),
        )
        self.assertIsNone(self.clock(first).report_waiting_since)
        rows = workflow_inactivity_context(user=self.superuser, params=QueryDict("mode=waiting"))[
            "rows"
        ]
        self.assertIsNone(rows[0]["days"])

    def test_handoff_resets_waiting_without_rewriting_original_work_dates(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        stage = S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=assignment,
            queued_at=self.today - timedelta(days=40),
            started_at=self.today - timedelta(days=30),
        )
        handoff_stage(
            self.text,
            self.superuser,
            stage_id=stage.pk,
            assigned_to_id=self.other_editor.pk,
            expected_assignment_id=assignment.pk,
            reason="Przekazanie",
        )
        stage.refresh_from_db()
        self.assertEqual(stage.waiting_reset_at, self.today)
        self.assertEqual(self.clock(stage).report_waiting_since, self.today)
        self.assertEqual(stage.handoffs.get().original_started_at, self.today - timedelta(days=30))

    def test_manual_correction_resets_only_changed_unfinished_work(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        stage = S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=assignment,
            queued_at=self.today - timedelta(days=40),
            started_at=self.today - timedelta(days=30),
        )
        done = S.objects.create(
            text=self.text,
            stage_type="editing",
            iteration=2,
            assignment=assignment,
            started_at=self.today - timedelta(days=35),
            ended_at=self.today - timedelta(days=31),
            is_completed=True,
        )
        correct_stage_performers(
            self.text.pk, {stage.pk: self.editor}, self.superuser, version_of(self.text)
        )
        stage.refresh_from_db()
        self.assertIsNone(stage.waiting_reset_at)
        correct_stage_performers(
            self.text.pk, {stage.pk: self.other_editor}, self.superuser, version_of(self.text)
        )
        stage.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(stage.waiting_reset_at, self.today)
        self.assertEqual(stage.started_at, self.today - timedelta(days=30))
        self.assertEqual(self.clock(stage).report_active_since, self.today)
        self.assertIsNone(done.waiting_reset_at)

    def test_assignment_save_resets_clock_only_on_real_performer_change(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.editor)
        stage = S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=assignment,
            queued_at=self.today - timedelta(days=40),
        )
        assignment.notes = "Notatka"
        assignment.save(update_fields=["notes"])
        stage.refresh_from_db()
        self.assertIsNone(stage.waiting_reset_at)
        assignment.assigned_to = self.other_editor
        assignment.save(update_fields=["assigned_to"])
        stage.refresh_from_db()
        self.assertEqual(stage.waiting_reset_at, self.today)
