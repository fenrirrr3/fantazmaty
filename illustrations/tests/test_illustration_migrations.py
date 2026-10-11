"""Migracje ilustracji: przejście na wielu ilustratorów zachowuje dawne kredyty, a migracja danych
tworzy tylko brakujące wpisy przestrzeni roboczej."""
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag
from django.utils import timezone

from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


@tag('migration', 'slow')
class IllustratorMigrationTests(TransactionTestCase):
    def test_existing_credit_and_manual_details_survive_upgrade(self):
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        # Migrating one app back also unapplies later migrations of other apps that depend on it.
        before = [('illustrations', '0005_independent_illustrator_directory')]
        executor.migrate(before)
        try:
            # The real database state: other apps keep their unrelated later migrations.
            state_apps = MigrationExecutor(connection)._create_project_state(
                with_applied_migrations=True).apps
            OldAnthology = state_apps.get_model('texts', 'Anthology')
            OldText = state_apps.get_model('texts', 'Text')
            OldIllustrator = state_apps.get_model('illustrations', 'Illustrator')
            OldIllustration = state_apps.get_model('illustrations', 'Illustration')
            book = OldAnthology.objects.create(title='Migracja ilustracji', status='ready')
            story = OldText.objects.create(title='Dawny tekst', anthology=book, length=10)
            artist = OldIllustrator.objects.create(first_name='Dawny', last_name='Ilustrator')
            row = OldIllustration.objects.create(
                text=story, illustrator=artist, status='delivered', assigned_at='2020-01-02',
                coordinator_notes='Notatka', illustrated_excerpt='Fragment',
                story_url='https://example.org/a')
            manual_story = OldText.objects.create(title='Ręczny', anthology=book, length=10)
            manual = OldIllustration.objects.create(text=manual_story, status='delivered',
                                                    manual_illustrator_name='Ręczny wpis')
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            NewIllustration = executor.loader.project_state(after).apps.get_model(
                'illustrations', 'Illustration')
            updated = NewIllustration.objects.get(pk=row.pk)
            self.assertEqual(list(updated.illustrators.values_list('pk', flat=True)), [artist.pk])
            self.assertEqual(str(updated.assigned_at), '2020-01-02')
            self.assertEqual(
                (updated.status, updated.coordinator_notes, updated.illustrated_excerpt, updated.story_url),
                ('delivered', 'Notatka', 'Fragment', 'https://example.org/a'))
            updated_manual = NewIllustration.objects.get(pk=manual.pk)
            self.assertFalse(updated_manual.illustrators.exists())
            self.assertEqual(updated_manual.manual_illustrator_name, 'Ręczny wpis')
        finally:
            MigrationExecutor(connection).migrate(after)


class WorkspaceDataMigrationTests(TestCase):
    def test_migration_creates_only_missing_required_rows_and_is_repeatable(self):
        book = Anthology.objects.create(title="Ilustrowana", has_illustrations=True)
        text = Text.objects.create(title="Tytuł ilustracji", anthology=book, length=100)
        illustration = Illustration.objects.get(text=text)
        illustration.illustrators.set([Illustrator.objects.create(first_name="Artysta")])
        Illustration.objects.filter(pk=illustration.pk).update(
            status="assigned", assigned_at=timezone.localdate() - timedelta(days=20)
        )
        other = Text.objects.create(title="Brakująca ilustracja", anthology=book, length=100)
        Illustration.objects.filter(text=other).delete()
        skipped_book = Anthology.objects.create(title="Bez ilustracji", has_illustrations=False)
        skipped = Text.objects.create(title="Pomijany", anthology=skipped_book, length=100)
        before = Illustration.objects.get(pk=illustration.pk)
        migrate = import_module(
            "illustrations.migrations.0002_prepare_illustration_workspace"
        ).create_missing
        for _ in range(2):
            migrate(apps, SimpleNamespace(connection=connection))
        self.assertEqual(Illustration.objects.filter(text=other).count(), 1)
        self.assertFalse(Illustration.objects.filter(text=skipped).exists())
        after = Illustration.objects.get(pk=illustration.pk)
        self.assertEqual(
            (before.status, list(before.illustrators.all()), before.assigned_at),
            (after.status, list(after.illustrators.all()), after.assigned_at),
        )
