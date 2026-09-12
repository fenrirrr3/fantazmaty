from io import StringIO

from django.core.management import call_command, CommandError
from django.test import TestCase
from django.utils import timezone

from texts.models import Text, Anthology
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow import services as flow
from core.management.commands.seed_demo_texts import TARGETS


class SeedDemoTextsTests(TestCase):
    def test_requires_demo_team_without_partial_writes(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo_texts", stdout=StringIO())
        self.assertFalse(Text.objects.exists())
        self.assertFalse(Anthology.objects.exists())

    def test_stages_assignments_continuation_and_repeat(self):
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_demo_texts", stdout=StringIO())
        self.assertEqual(Text.objects.count(), 42)
        self.assertEqual(Anthology.objects.count(), 3)
        self.assertFalse(Text.objects.filter(authors__is_blacklisted=True).exists())
        self.assertFalse(Text.objects.filter(authors__isnull=True).exists())
        for index, target in enumerate(TARGETS):
            for variant in range(3):
                marker = f"[DEMO-WF-{index * 3 + variant + 1:03d}]"
                text = Text.objects.get(title__startswith=marker)
                stage = flow.current_stage_queryset(text).get(stage_type=target, is_completed=False)
                if target not in (WorkflowStage.StageType.READY_FOR_EDITING, WorkflowStage.StageType.STYLING):
                    self.assertIsNotNone(stage.started_at)
                for item in text.workflow_stages.all():
                    item.full_clean()
                for assignment in text.workflow_role_assignments.all():
                    assignment.full_clean()
        # Kontynuacja procesu na wygenerowanym tekście do samego końca.
        text = Text.objects.get(title__startswith="[DEMO-WF-040]")
        stage = text.workflow_stages.get(stage_type="styling", is_completed=False)
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_superuser(username="styling_admin", email="styling@example.com", password="test")
        stage = flow.claim_stage(text, "styling", user, started_at=timezone.localdate())
        flow.complete_stage(stage, user, timezone.localdate())
        before = WorkflowStage.objects.count()
        call_command("seed_demo_texts", stdout=StringIO())
        self.assertEqual(Text.objects.count(), 42)
        self.assertEqual(WorkflowStage.objects.count(), before)
        self.assertTrue(text.workflow_stages.filter(stage_type="ready", is_completed=False).exists())
