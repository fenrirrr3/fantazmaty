"""Ustawienia MySQL dla CI: import bez zmiennych środowiska produkcji."""
import os
import subprocess
import sys

from django.test import SimpleTestCase


class MySQLSettingsBootstrapTests(SimpleTestCase):
    def test_ci_settings_import_without_production_environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('DJANGO_')}
        env.update(TEST_MYSQL_USER='isolated_test', TEST_MYSQL_PASSWORD='isolated_test')
        check = ('from fantazmaty.mysql_integration_settings import DATABASES; '
                 'assert DATABASES["default"]["ENGINE"] == "django.db.backends.mysql"; print("OK")')
        result = subprocess.run([sys.executable, '-c', check], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'OK')
