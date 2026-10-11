"""Polityka edycji widoków i tokeny wersji: wersja rośnie przy zapisie, aktualizacjach zbiorczych i bulk_create."""
from django.apps import apps
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import URLResolver, get_resolver

from authors.models import BlacklistEntry
from core.edit_policy import policy_for
from core.edit_versions import PARENTS, TRACKED, VersionedQuerySet, batched_bumps, version_of
from core.models import MailboxConnection
from texts.models import Anthology, Text, TextTranslation
from workflow.models import WorkflowStage


def core_patterns():
    def walk(resolver, namespace=None):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, pattern.namespace or namespace)
            else:
                yield namespace, pattern
    return {pattern.name: pattern for namespace, pattern in walk(get_resolver()) if namespace == 'core'}


class EditPolicyTests(SimpleTestCase):
    """Polityki edycji są deklarowane przy widokach (middleware nie trzyma list nazw adresów)."""

    # Formularze, których POST musi nieść aktualny token _edit_version.
    VERSIONED = {
        'update_text_tags', 'update_text_audiobook', 'cancel_workflow_repetition',
        'handoff_workflow_stage', 'link_text_review', 'set_text_authors', 'set_translators',
        'update_coordinator_note', 'update_text_content_warnings', 'edit_text_note',
        'delete_text_note', 'update_text_file', 'update_review_file',
        'update_review_content_warnings', 'update_author_notification', 'update_review_status',
        'assigned_review_detail',
    }
    # Widoki, które sprawdzają uprawnienia do obiektu przed założeniem blokady edycji.
    OBJECT_CHECKS = {
        'anthology_detail', 'add_text_note', 'update_text_content_warnings', 'edit_text_note',
        'delete_text_note', 'assigned_review_detail', 'update_review_content_warnings',
        'edit_vacation', 'end_vacation', 'cancel_vacation',
    }

    def test_versioned_forms_require_token(self):
        patterns = core_patterns()
        for name in sorted(self.VERSIONED):
            with self.subTest(name=name):
                policy = getattr(patterns[name].callback, 'edit_policy', None)
                self.assertIsNotNone(policy)
                self.assertTrue(policy.require_version)

    def test_object_level_checks_are_declared(self):
        patterns = core_patterns()
        for name in sorted(self.OBJECT_CHECKS):
            with self.subTest(name=name):
                policy = getattr(patterns[name].callback, 'edit_policy', None)
                self.assertIsNotNone(policy)
                self.assertIsNotNone(policy.check)

    def test_policy_lookup_tolerates_missing_match(self):
        self.assertIsNone(policy_for(None))


class VersionedModelTests(TestCase):
    def test_every_tracked_model_uses_versioned_queryset(self):
        # Modele auth należą do Django; ich nieliczne bezpośrednie aktualizacje podbijają wersję jawnie.
        exempt = {'auth.user', 'auth.group'}
        missing = []
        for label in sorted((TRACKED | set(PARENTS)) - exempt):
            queryset_class = apps.get_model(label)._default_manager._queryset_class
            if not issubclass(queryset_class, VersionedQuerySet):
                missing.append(label)
        self.assertEqual(missing, [])

    def test_blacklist_and_mailbox_are_versioned(self):
        """Regresja: zapis wpisu czarnej listy i skrzynki pocztowej nie zmieniał tokenu wersji."""
        for obj in (
            BlacklistEntry.objects.create(name='Osoba'),
            MailboxConnection.objects.create(name='Teksty', host='example.test', username='teksty'),
        ):
            before = version_of(obj)
            obj.save()
            self.assertGreater(version_of(obj), before)


class BulkWriteVersionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Antologia')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)

    def stage(self):
        return WorkflowStage.objects.create(
            text=self.text, workflow_cycle=self.text.current_workflow_cycle,
            stage_type=WorkflowStage.StageType.READY_FOR_EDITING, iteration=1)

    def test_queryset_update_bumps_record_and_parent(self):
        stage = self.stage()
        text_before, stage_before = version_of(self.text), version_of(stage)
        WorkflowStage.objects.filter(pk=stage.pk).update(is_released=False)
        self.assertGreater(version_of(self.text), text_before)
        self.assertGreater(version_of(stage), stage_before)

    def test_related_manager_update_bumps_parent(self):
        self.stage()
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
        bumps = [q for q in queries.captured_queries
                 if 'core_editrevision' in q['sql'] and q['sql'].startswith('UPDATE')]
        self.assertEqual(len(bumps), 1)
        self.assertEqual(version_of(self.text), before + 1)
