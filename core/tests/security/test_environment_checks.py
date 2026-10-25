"""Kontrole środowiska: ustawienia MySQL dla CI bez zmiennych produkcji i ostrzeżenie o niezgodnej wersji Django."""
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
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


class OptionalEnvironmentSettingsTests(SimpleTestCase):
    def test_optional_variables_are_read_by_settings_at_startup(self):
        """Zmienne dawniej czytane w kodzie trafiają do fantazmaty.settings z tymi samymi domyślnymi wartościami
        (webhooki Discorda pod nazwą z „SECRET”, którą Django ukrywa na stronach błędów)."""
        settings_names = {'CMS_BASE_URL': 'CMS_BASE_URL', 'DISCORD_WEBHOOKS': 'DISCORD_WEBHOOK_SECRETS',
                          'ACTIVITY_SPOOL_DIR': 'ACTIVITY_SPOOL_DIR', 'AUTH_THROTTLE_ENABLED': 'AUTH_THROTTLE_ENABLED',
                          'ACCOUNT_INVITATION_DAYS': 'ACCOUNT_INVITATION_DAYS',
                          'DOCUMENT_CONVERTER_PYTHON': 'DOCUMENT_CONVERTER_PYTHON'}
        names = tuple(settings_names)
        env = {k: v for k, v in os.environ.items() if k not in names and not k.startswith('DJANGO_')}
        env.update(DJANGO_SKIP_DOTENV='1', DJANGO_SECRET_KEY='isolated-tests-only-' + 'x' * 40,
                   DJANGO_DB_NAME='unused', DJANGO_DB_USER='unused', DJANGO_DB_PASSWORD='unused')
        check = ('import json; from fantazmaty import settings as s; '
                 f'print(json.dumps([str(getattr(s, name)) for name in {tuple(settings_names.values())!r}]))')
        cases = {
            'domyślne': ({}, ['https://cms.fantazmaty.pl', '{}', str(Path(settings.BASE_DIR) / 'var' / 'activity-spool'),
                              'True', '7', '']),
            'ze środowiska': (dict(zip(names, ('https://cms.example', '{"Kanał": "x"}', '/tmp/spool', 'false', '14',
                                                '/usr/bin/python3'))),
                              ['https://cms.example', '{"Kanał": "x"}', '/tmp/spool', 'False', '14', '/usr/bin/python3']),
        }
        for case, (values, expected) in cases.items():
            with self.subTest(case):
                result = subprocess.run([sys.executable, '-c', check], env={**env, **values}, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)


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
