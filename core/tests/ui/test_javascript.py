"""Zachowanie skryptów interfejsu: uruchamia wszystkie testy z core/js_tests (wymaga Node.js)."""
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase, tag

SCRIPTS = sorted((Path(settings.BASE_DIR) / 'core' / 'js_tests').glob('*.cjs'))


@tag('js')
class JavaScriptBehaviourTests(SimpleTestCase):
    @skipUnless(shutil.which('node'), 'Testy JavaScript wymagają Node.js.')
    def test_every_script_passes(self):
        self.assertTrue(SCRIPTS)
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run([shutil.which('node'), str(script)], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
