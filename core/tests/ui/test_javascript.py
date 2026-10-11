"""Zachowanie skryptów interfejsu: uruchamia wszystkie testy z core/js_tests (wymaga Node.js)."""
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase, tag

JS_TESTS = Path(settings.BASE_DIR) / 'core' / 'js_tests'
# Only top-level *.cjs files are tests; js_tests/support/ holds the shared fake DOM.
SCRIPTS = sorted(JS_TESTS.glob('*.cjs'))
STATIC_SCRIPTS = sorted((Path(settings.BASE_DIR) / 'core' / 'static' / 'core').glob('*.js'))


@tag('js')
class JavaScriptBehaviourTests(SimpleTestCase):
    @skipUnless(shutil.which('node'), 'Testy JavaScript wymagają Node.js.')
    def test_every_script_passes(self):
        self.assertTrue(SCRIPTS)
        node = shutil.which('node')
        run = partial(subprocess.run, capture_output=True, text=True, timeout=60)
        # Skrypty są niezależne; równoległe uruchomienie skraca czas startu kolejnych procesów Node.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = pool.map(lambda script: run([node, str(script)]), SCRIPTS)
            for script, result in zip(SCRIPTS, results):
                with self.subTest(script=script.name):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_interface_script_has_a_behaviour_test(self):
        """Nowy skrypt w core/static/core musi mieć test w core/js_tests (odwołanie do nazwy pliku)."""
        sources = '\n'.join(script.read_text(encoding='utf-8') for script in SCRIPTS)
        untested = [script.name for script in STATIC_SCRIPTS
                    if not re.search(rf"(?<![\w.-]){re.escape(script.name)}'", sources)]
        self.assertEqual(untested, [])
