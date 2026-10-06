from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class IllustratorMigrationTests(TransactionTestCase):
    def test_existing_credit_and_manual_details_survive_upgrade(self):
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        before = [(app, '0005_independent_illustrator_directory' if app == 'illustrations' else name) for app, name in after]
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            Anthology = apps.get_model('texts', 'Anthology')
            Text = apps.get_model('texts', 'Text')
            Illustrator = apps.get_model('illustrations', 'Illustrator')
            Illustration = apps.get_model('illustrations', 'Illustration')
            book = Anthology.objects.create(title='Migracja ilustracji', status='ready')
            story = Text.objects.create(title='Dawny tekst', anthology=book, length=10)
            artist = Illustrator.objects.create(first_name='Dawny', last_name='Ilustrator')
            row = Illustration.objects.create(text=story, illustrator=artist, status='delivered',
                assigned_at='2020-01-02', coordinator_notes='Notatka', illustrated_excerpt='Fragment', story_url='https://example.org/a')
            manual_story = Text.objects.create(title='Ręczny', anthology=book, length=10)
            manual = Illustration.objects.create(text=manual_story, status='delivered', manual_illustrator_name='Ręczny wpis')
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            apps = executor.loader.project_state(after).apps
            updated = apps.get_model('illustrations', 'Illustration').objects.get(pk=row.pk)
            self.assertEqual(list(updated.illustrators.values_list('pk', flat=True)), [artist.pk])
            self.assertEqual(str(updated.assigned_at), '2020-01-02')
            self.assertEqual((updated.status, updated.coordinator_notes, updated.illustrated_excerpt, updated.story_url),
                ('delivered', 'Notatka', 'Fragment', 'https://example.org/a'))
            updated_manual = apps.get_model('illustrations', 'Illustration').objects.get(pk=manual.pk)
            self.assertFalse(updated_manual.illustrators.exists())
            self.assertEqual(updated_manual.manual_illustrator_name, 'Ręczny wpis')
        finally:
            MigrationExecutor(connection).migrate(after)
