"""Program lokalny (jeden plik): budowa z modułów core/services i działanie wszystkich czterech programów
bez Django."""
import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, tag
from docx import Document

from .support import saved

BUILD = Path(__file__).resolve().parents[3] / 'tools' / 'redaktor_docx' / 'build.py'


def load_bundle(path):
    spec = importlib.util.spec_from_file_location('redaktor_docx_bundle', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['redaktor_docx_bundle'] = module
    spec.loader.exec_module(module)
    return module


@tag('slow')
class StandaloneBundleTests(SimpleTestCase):
    def test_bundle_builds_and_runs_every_program_without_django(self):
        spec = importlib.util.spec_from_file_location('redaktor_docx_build', BUILD)
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'redaktor_docx.py'
            build.build(target)
            source = target.read_text(encoding='utf-8')
            self.assertNotIn('from .document_', source)
            self.assertNotIn("Path(__file__).with_name('fonts')", source)
            bundle = load_bundle(target)
            import os
            os.environ['XDG_CONFIG_HOME'] = str(Path(folder) / 'config')
            document = Document()
            document.add_paragraph('Koty i koty "siedzą"  obok ,tak. Pickup stał - powiedział.')
            document.add_paragraph('Drugi akapit z kotem.')
            docx = Path(folder) / 'tekst.docx'
            docx.write_bytes(saved(document).getvalue())
            settings, warning = bundle.load_settings()
            self.assertEqual(warning, '')
            progress = []
            results = {}
            for program in bundle.PROGRAMS:
                result, notes = bundle.run_program(program, docx, settings, progress.append)
                results[program] = (result, notes)
            cleaned = Document(results['Odkurzacz'][0]).paragraphs[0].text
            self.assertEqual(cleaned, 'Koty i koty „siedzą” obok, tak. Pikap stał – powiedział.')
            self.assertIn('Podsumowanie zmian', results['Odkurzacz'][1][0])
            pdf, epub = results['Konwersja'][0]
            self.assertTrue(pdf.stat().st_size > 1000 and epub.stat().st_size > 1000)
            coloured = Document(results['Powtórzenia'][0])
            self.assertTrue(coloured.element.body.xpath('.//w:color'))
            self.assertEqual(Document(results['Przebudowa'][0]).paragraphs[1].text, 'Drugi akapit z kotem.')
            self.assertTrue(progress)
            # Ustawienia i czcionki trafiają do katalogu konfiguracji; słownik zamian ma format tekstowy.
            bundle.save_settings(settings)
            stored = json.loads((Path(folder) / 'config' / 'RedaktorDOCX' / 'ustawienia.json').read_text(encoding='utf-8'))
            self.assertEqual(stored['rules'], settings['rules'])
            self.assertEqual(bundle.parse_replacements('kot = pies\nre:\\bpsa\\b = kota\n# uwaga')[1],
                             {'kind': 'pattern', 'source': r'\bpsa\b', 'target': 'kota'})
            self.assertTrue((Path(folder) / 'config' / 'RedaktorDOCX' / 'fonts' / 'NimbusRoman-Regular.ttf').exists())
