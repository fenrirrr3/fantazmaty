"""Edit-version tokens must change for bulk writes as well as model saves."""
from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.edit_versions import PARENTS, TRACKED, VersionedQuerySet, batched_bumps, version_of
from texts.models import Anthology, Text, TextTranslation
from workflow.models import WorkflowStage


class VersionedQuerySetCoverageTests(TestCase):
    def test_every_tracked_model_uses_versioned_queryset(self):
        # auth models belong to Django; their few direct updates bump explicitly.
        exempt = {'auth.user', 'auth.group'}
        missing = []
        for label in sorted((TRACKED | set(PARENTS)) - exempt):
            queryset_class = apps.get_model(label)._default_manager._queryset_class
            if not issubclass(queryset_class, VersionedQuerySet):
                missing.append(label)
        self.assertEqual(missing, [])


class BulkWriteVersionTests(TestCase):
    def setUp(self):
        self.book = Anthology.objects.create(title='Antologia')
        self.text = Text.objects.create(title='Tekst', length=100, anthology=self.book)

    def test_queryset_update_bumps_record_and_parent(self):
        stage = WorkflowStage.objects.create(
            text=self.text, workflow_cycle=self.text.current_workflow_cycle,
            stage_type=WorkflowStage.StageType.READY_FOR_EDITING, iteration=1)
        text_before, stage_before = version_of(self.text), version_of(stage)
        WorkflowStage.objects.filter(pk=stage.pk).update(is_released=False)
        self.assertGreater(version_of(self.text), text_before)
        self.assertGreater(version_of(stage), stage_before)

    def test_related_manager_update_bumps_parent(self):
        WorkflowStage.objects.create(
            text=self.text, workflow_cycle=self.text.current_workflow_cycle,
            stage_type=WorkflowStage.StageType.READY_FOR_EDITING, iteration=1)
        before = version_of(self.text)
        self.text.workflow_stages.update(is_released=False)
        self.assertGreater(version_of(self.text), before)

    def test_empty_update_does_not_bump(self):
        before = version_of(self.text)
        WorkflowStage.objects.filter(text=self.text).update(is_released=False)
        self.assertEqual(version_of(self.text), before)

    def test_bulk_create_bumps_parent(self):
        before = version_of(self.text)
        TextTranslation.objects.bulk_create([TextTranslation(text=self.text)])
        self.assertGreater(version_of(self.text), before)

    def test_batched_bumps_write_each_record_once(self):
        before = version_of(self.text)
        with CaptureQueriesContext(connection) as queries:
            with batched_bumps():
                for _ in range(5):
                    self.text.save(update_fields=['title'])
        bumps = [q for q in queries.captured_queries if 'core_editrevision' in q['sql'] and q['sql'].startswith('UPDATE')]
        self.assertEqual(len(bumps), 1)
        self.assertEqual(version_of(self.text), before + 1)
