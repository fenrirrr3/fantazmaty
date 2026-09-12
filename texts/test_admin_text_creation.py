from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from authors.models import Author
from texts.models import Text
from workflow.models import WorkflowStage


class TextAdminCreationTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser(
            username="admin_text_creation", email="admin@example.com", password="Test-password-174!"
        )
        self.client.force_login(user)
        self.author = Author.objects.create(first_name="Anna", last_name="Testowa", email="author@example.com")

    def data(self):
        return {
            "title": "Nowy tekst", "authors": [str(self.author.pk)], "length": "25000",
            "workflow_role_assignments-TOTAL_FORMS": "0",
            "workflow_role_assignments-INITIAL_FORMS": "0",
            "workflow_role_assignments-MIN_NUM_FORMS": "0",
            "workflow_role_assignments-MAX_NUM_FORMS": "1000",
            "workflow_stages-TOTAL_FORMS": "0",
            "workflow_stages-INITIAL_FORMS": "0",
            "workflow_stages-MIN_NUM_FORMS": "0",
            "workflow_stages-MAX_NUM_FORMS": "1000",
            "_save": "Zapisz",
        }

    def test_add_without_inlines_starts_ready_for_editing(self):
        response = self.client.post(reverse("admin:texts_text_add"), self.data())
        self.assertEqual(response.status_code, 302)
        text = Text.objects.get(title="Nowy tekst")
        stage = text.workflow_stages.get()
        self.assertEqual(stage.stage_type, WorkflowStage.StageType.READY_FOR_EDITING)
        self.assertEqual(stage.workflow_cycle, text.current_workflow_cycle)
        self.assertFalse(stage.is_completed)
        self.assertIsNone(stage.started_at)

    def test_explicit_stage_is_preserved_without_extra_default(self):
        data = self.data()
        data.update({"workflow_stages-TOTAL_FORMS": "1",
                     "workflow_stages-0-stage_type": "editing",
                     "workflow_stages-0-iteration": "1"})
        response = self.client.post(reverse("admin:texts_text_add"), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Text.objects.get().workflow_stages.get().stage_type, "editing")

    def test_invalid_submission_creates_neither_text_nor_stage(self):
        data = self.data()
        data["title"] = ""
        response = self.client.post(reverse("admin:texts_text_add"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Text.objects.exists())
        self.assertFalse(WorkflowStage.objects.exists())
