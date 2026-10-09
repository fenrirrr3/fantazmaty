from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless
from django.test import SimpleTestCase


class FilterButtonsTests(SimpleTestCase):
    @skipUnless(shutil.which('node'), 'Test filtrów wymaga Node.js.')
    def test_filter_buttons_keep_multiselection_and_clear_author_flag(self):
        script = Path(__file__).with_name('js_tests') / 'filter_buttons.cjs'
        result = subprocess.run([shutil.which('node'), str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
