"""Migracja illustrations 0012: wytyczne dla ilustratorów trafiają do pustego pola, nie nadpisują własnych."""
from importlib import import_module

from django.apps import apps
from django.test import TestCase

from illustrations.models import PublicIllustrationSettings

migration = import_module('illustrations.migrations.0012_illustration_guidelines_content')


class GuidelinesContentMigrationTests(TestCase):
    def test_fills_only_empty_and_reverses_only_own_text(self):
        cases = [('puste', '', migration.GUIDELINES, ''),
                 ('same spacje', '  \n', migration.GUIDELINES, ''),
                 ('własne', '# Moje wytyczne', '# Moje wytyczne', '# Moje wytyczne')]
        for label, start, after_fill, after_clear in cases:
            with self.subTest(label):
                PublicIllustrationSettings.objects.update_or_create(pk=1, defaults={'guidelines': start})
                migration.fill_guidelines(apps, None)
                self.assertEqual(PublicIllustrationSettings.objects.get(pk=1).guidelines, after_fill)
                migration.clear_guidelines(apps, None)
                self.assertEqual(PublicIllustrationSettings.objects.get(pk=1).guidelines, after_clear)

    def test_text_has_structure_of_the_document(self):
        text = migration.GUIDELINES
        for fragment in ('# Wytyczne dla ilustratorów', '# Organizacja pracy', '# Ilustracja okładkowa',
                         '- Nie przyjmujemy prac tworzonych za pomocą AI.', '[tutaj](https://cms.fantazmaty.pl/ilustracje/zewnetrzne/)'):
            with self.subTest(fragment):
                self.assertIn(fragment, text)
