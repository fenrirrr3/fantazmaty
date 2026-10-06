import json
from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import SimpleTestCase
from docx import Document
from docx.oxml import OxmlElement
from core.services.document_converter import run_converter, ConversionError


class ConverterDiagnosticsTests(SimpleTestCase):
    def test_worker_exposes_only_known_reason_not_document_content(self):
        for reason in ('analysis_limit', 'tracked_changes'):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                directory = Path(directory)
                doc = Document()
                doc.add_paragraph('PRYWATNY_FRAGMENT' + ('x'*20001 if reason == 'analysis_limit' else ''))
                if reason == 'tracked_changes':
                    doc.element.body.append(OxmlElement('w:ins'))
                doc.save(directory/'source.docx')
                (directory/'job.json').write_text(json.dumps({'formats': [], 'include_docx': True, 'repetitions': {}}), encoding='utf-8')
                with self.assertRaises(ConversionError) as error:
                    run_converter(directory, 30)
                report = json.loads((directory/'error.json').read_text(encoding='utf-8'))
                self.assertEqual(report['public_code'], reason)
                self.assertNotIn('PRYWATNY_FRAGMENT', json.dumps(report) + str(error.exception))
                self.assertNotIn('ValueError', str(error.exception))
