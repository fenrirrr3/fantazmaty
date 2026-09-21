from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from core.tests import CoreTestDataMixin
from core.workflow_tokens import make_token, check_token
from texts.models import Text
from workflow.models import WorkflowRoleAssignment


class Cleanup24Tests(CoreTestDataMixin, TestCase):
    def test_legacy_models_removed(self):
        for label in ("texts.HistoricalTextAssignment", "core.DiscordDispatch"):
            with self.assertRaises(LookupError):
                apps.get_model(label)
        names = {field.name for field in Text._meta.fields}
        self.assertNotIn("is_historical", names)
        self.assertIn("import_source", names)
        self.assertIn("import_source_row", names)

    def test_token_scoped_and_invalidated_by_assignment(self):
        text = Text.objects.create(title="Wersja 24", anthology=self.anthology, length=100)
        token = make_token(text, self.superuser)
        check_token(token, text, self.superuser)
        with self.assertRaises(ValidationError):
            check_token(token, text, self.editor)
        WorkflowRoleAssignment.objects.create(text=text, role="editor", assigned_to=self.editor)
        with self.assertRaises(ValidationError):
            check_token(token, text, self.superuser)

    def test_inactive_imported_executor_profile_accessible(self):
        from workflow.completed_import import import_completed_workflow
        text = Text.objects.create(title="Import 24", anthology=self.anthology, length=100)
        self.editor.email = "import24@example.com"
        self.editor.save()
        import_completed_workflow(text_id=text.pk, stages=[{"stage_type": "editing", "assigned_to": self.editor.email}], next_stage="ready")
        person = self.editor.person_profile
        person.is_active = False
        person.save()
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("core:person_detail", args=[person.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import 24")
