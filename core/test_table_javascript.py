from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless
from django.test import SimpleTestCase


class TableJavaScriptTests(SimpleTestCase):
    @skipUnless(shutil.which('node'), 'Test zachowania JavaScript wymaga Node.js.')
    def test_table_sorting_and_shift_selection(self):
        script = Path(__file__).with_name('js_tests') / 'table_behaviour.cjs'
        result = subprocess.run([shutil.which('node'), str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
