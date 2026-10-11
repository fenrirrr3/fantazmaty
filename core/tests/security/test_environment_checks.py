"""Kontrole środowiska: ustawienia MySQL dla CI bez zmiennych produkcji i ostrzeżenie o niezgodnej wersji Django."""
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from core.runtime_checks import pinned_django_version, runtime_version


class MySQLSettingsBootstrapTests(SimpleTestCase):
    def test_ci_settings_import_without_production_environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('DJANGO_')}
        env.update(TEST_MYSQL_USER='isolated_test', TEST_MYSQL_PASSWORD='isolated_test')
        check = ('from fantazmaty.mysql_integration_settings import DATABASES; '
                 'assert DATABASES["default"]["ENGINE"] == "django.db.backends.mysql"; print("OK")')
        result = subprocess.run([sys.executable, '-c', check], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'OK')


class DjangoVersionCheckTests(SimpleTestCase):
    def check_with_requirements(self, content):
        with TemporaryDirectory() as directory, self.settings(BASE_DIR=Path(directory)):
            if content is not None:
                (Path(directory) / 'requirements.txt').write_text(content, encoding='utf-8')
            return pinned_django_version(), runtime_version(None)

    def test_mismatched_pinned_version_produces_a_warning(self):
        pinned, warnings = self.check_with_requirements('lxml==5.0\nDjango==1.0.0\n')
        self.assertEqual(pinned, '1.0.0')
        self.assertEqual([warning.id for warning in warnings], ['core.W008'])
        self.assertIn('1.0.0', warnings[0].msg)

    def test_missing_file_or_unpinned_django_is_silent(self):
        self.assertEqual(self.check_with_requirements(None), (None, []))
        self.assertEqual(self.check_with_requirements('django>=5\n'), (None, []))

    def test_installed_pinned_version_passes(self):
        import django
        self.assertEqual(self.check_with_requirements(f'django=={django.get_version()}\n')[1], [])
