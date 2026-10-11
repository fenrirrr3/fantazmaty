"""Konwerter DOCX → PDF/EPUB: proces roboczy i jego diagnostyka, uwagi konwersji, wynik PDF i rozdziały EPUB."""
import json
import runpy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import unquote
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, tag
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.shared import Cm
from fpdf import FPDF
from lxml import etree
from lxml import html as html_parser

import core.services
from core.program_test_support import run_program
from core.services.document_conversion_worker import convert, polish_warnings, split_chapters
from core.services.document_converter import (
    ConversionError, conversion_filename, convert_document, run_converter,
)
from core.services.document_pdf import render_pdf
from fantazmaty.testing import create_member

from .support import IsolatedProgramDirsMixin, downloaded, formatted_docx, saved, typography_sample


def pdf_html_spy(captured):
    """Podmiana FPDF.write_html zapisująca przekazany HTML i pozycję kursora."""
    original = FPDF.write_html

    def spy(pdf, content, **kwargs):
        captured.append((content, pdf.y))
        return original(pdf, content, **kwargs)
    return patch.object(FPDF, 'write_html', spy)


class ConverterWorkerTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_worker_exposes_only_known_reason_not_document_content(self):
        for reason in ('analysis_limit', 'tracked_changes'):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                directory = Path(directory)
                doc = Document()
                doc.add_paragraph('PRYWATNY_FRAGMENT' + ('x' * 20001 if reason == 'analysis_limit' else ''))
                if reason == 'tracked_changes':
                    doc.element.body.append(OxmlElement('w:ins'))
                doc.save(directory / 'source.docx')
                (directory / 'job.json').write_text(
                    json.dumps({'formats': [], 'include_docx': True, 'repetitions': {}}), encoding='utf-8')
                with self.assertRaises(ConversionError) as error:
                    run_converter(directory, 30)
                report = json.loads((directory / 'error.json').read_text(encoding='utf-8'))
                self.assertEqual(report['public_code'], reason)
                self.assertNotIn('PRYWATNY_FRAGMENT', json.dumps(report) + str(error.exception))
                self.assertNotIn('ValueError', str(error.exception))

    def test_bootstrap_records_failures_before_worker_initialization(self):
        """Regresja: błąd importu przed startem procesu roboczego ujawniał treść wyjątku."""
        bootstrap = Path(core.services.__file__).with_name('document_worker_bootstrap.py')
        runner = runpy.run_path
        with TemporaryDirectory() as tmp:
            with (
                patch('sys.argv', ['bootstrap', tmp]),
                patch('runpy.run_path', side_effect=ImportError('PRIVATE credential')),
            ):
                with self.assertRaises(SystemExit) as caught:
                    runner(str(bootstrap), run_name='__main__')
            self.assertEqual(caught.exception.code, 2)
            report = (Path(tmp) / 'error.json').read_text()
        self.assertNotIn('PRIVATE', report)
        self.assertEqual(json.loads(report)['error'], 'ImportError')

    def test_warnings_are_returned_with_a_single_file_not_an_archive(self):
        """Regresja: uwagi konwertera zamieniały pojedynczy plik w archiwum ZIP."""
        cases = (
            ('epub', b'EPUB test output', lambda: BytesIO(formatted_docx()), {}),
            ('pdf', b'%PDF-test', lambda: SimpleUploadedFile('Zażółć.docx', formatted_docx()),
             {'preserve_filename': True}),
        )
        for extension, content, source, options in cases:
            def worker(directory, timeout):
                (directory / f'document.{extension}').write_bytes(content)
                (directory / 'warnings.json').write_text(json.dumps(['Pominięto nieobsługiwany element.']))

            with self.subTest(extension=extension), \
                    patch('core.services.document_converter.run_converter', side_effect=worker):
                result, result_extension, _ = convert_document(source(), [extension], **options)
                with result:
                    self.assertEqual(result_extension, extension)
                    self.assertEqual(result.read(), content)
                self.assertEqual(result.conversion_warnings, ['Pominięto nieobsługiwany element.'])

    def test_names_cannot_escape_zip_or_inject_response_headers(self):
        self.assertEqual(conversion_filename(r'C:\folder\Tytuł.v1.docx', 'pdf'), 'Tytuł.v1.pdf')
        self.assertEqual(conversion_filename('../../tekst\r\n.docx', 'epub'), 'tekst.epub')

    @tag('slow')
    def test_conversion_epub_and_pdf_preserve_formatting(self):
        result, _, _ = convert_document(typography_sample(), ['pdf', 'epub'])
        with result, ZipFile(result) as archive:
            self.assertTrue(archive.read('document.pdf').startswith(b'%PDF'))
            with ZipFile(BytesIO(archive.read('document.epub'))) as epub:
                content = epub.read('EPUB/content.xhtml').decode()
                css = epub.read('EPUB/style.css').decode()
        self.assertIn('align-center', content)
        self.assertIn('<strong>', content)
        self.assertIn('<em>', content)
        self.assertIn('1.25cm', css)
        self.assertIn('Times New Roman', css)

    def test_epub_preserves_empty_paragraphs_and_removes_work_folder(self):
        doc = Document()
        doc.add_paragraph('A')
        doc.add_paragraph()
        doc.add_paragraph()
        doc.add_paragraph('B')
        out, _, _ = convert_document(saved(doc), ['epub'])
        with out, ZipFile(out) as archive:
            root = etree.fromstring(archive.read('EPUB/content.xhtml'))
        paragraphs = root.findall('.//{http://www.w3.org/1999/xhtml}p')
        self.assertEqual([''.join(p.itertext()).strip() for p in paragraphs], ['A', '', '', 'B'])
        self.assertFalse(list((self.work_dir / 'convert').glob('document-*')))

    @tag('slow')
    def test_empty_paragraphs_survive_cleaning_and_html_conversion(self):
        """Regresja: Odkurzacz i konwersja do HTML zjadały puste akapity przed PDF."""
        doc = Document()
        doc.add_paragraph('Pierwszy.')
        doc.add_paragraph()
        doc.add_paragraph()
        doc.add_paragraph('Drugi.').alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        def inspect_worker(directory, timeout):
            self.assertEqual([p.text for p in Document(directory / 'source.docx').paragraphs],
                             ['Pierwszy.', '', '', 'Drugi.'])
            return run_converter(directory, timeout)

        with patch('core.services.document_converter.run_converter', side_effect=inspect_worker):
            output, _, _ = convert_document(saved(doc), ['pdf'], use_cleaner=True)
        with output:
            self.assertTrue(output.read().startswith(b'%PDF'))
        source = self.work_dir / 'input.docx'
        doc.save(source)
        with patch('core.services.document_pdf.render_pdf') as pdf:
            convert(source, self.work_dir, ['pdf'], 'Test')
        html = pdf.call_args.args[2]
        paragraphs = html_parser.fragment_fromstring(html, create_parent='div').xpath('.//p')
        self.assertEqual(sum(not p.text_content().strip() for p in paragraphs), 2)
        self.assertIn('align-justify', html)


class PdfRenderingTests(SimpleTestCase):
    def test_pdf_receives_justification_and_empty_lines(self):
        doc = Document()
        doc.add_paragraph('Justowany tekst.').alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        rendered = []
        with TemporaryDirectory() as tmp, pdf_html_spy(rendered):
            source = Path(tmp) / 'input.docx'
            doc.save(source)
            render_pdf(source, Path(tmp) / 'output.pdf', '<p>Justowany tekst.</p><p></p><p></p><p>Koniec</p>',
                       {}, 'Test')
        self.assertIn('align="justify"', rendered[0][0])
        self.assertGreaterEqual(rendered[1][1] - rendered[0][1], 18.9)

    def test_pdf_nested_paragraphs_receive_geometry(self):
        """Regresja: akapity w listach i tabelach traciły wyrównanie, wcięcie i interlinię w PDF."""
        doc = Document()
        listed = doc.add_paragraph('Lista', style='List Number')
        listed.alignment = WD_ALIGN_PARAGRAPH.CENTER
        listed.paragraph_format.first_line_indent = Cm(0)
        listed.paragraph_format.line_spacing = 2
        cell = doc.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
        cell.text = 'Tabela'
        cell.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        captured = []
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.docx'
            doc.save(source)
            with pdf_html_spy(captured):
                render_pdf(source, Path(tmp) / 'result.pdf',
                           '<ol><li>Lista</li></ol><table><tr><td><p>Tabela</p></td></tr></table>', {}, 'Test')
            self.assertTrue((Path(tmp) / 'result.pdf').read_bytes().startswith(b'%PDF'))
        tree = html_parser.fragment_fromstring(''.join(content for content, _ in captured), create_parent='div')
        listed = tree.xpath('.//li/p')[0]
        self.assertEqual(listed.get('align'), 'center')
        self.assertEqual(listed.get('data-indent'), '0')
        self.assertEqual(listed.get('line-height'), '2.0')
        self.assertEqual(tree.xpath('.//td/p')[0].get('align'), 'right')


class EpubPresentationTests(SimpleTestCase):
    def test_chapters_follow_the_repeated_heading_level(self):
        content = ('<h1 id="section-1">Tytuł</h1><p>Wstęp</p><h2 id="section-2">Rozdział 1</h2><p>A</p>'
                   '<h3 id="section-3">Część</h3><p>B</p><h2 id="section-4">Rozdział 2</h2><p>C</p>')
        chapters = split_chapters(content, 'Dokument')
        self.assertEqual([title for title, _, _ in chapters], ['Dokument', 'Rozdział 1', 'Rozdział 2'])
        self.assertEqual([anchor for anchor, _ in chapters[1][2]], ['section-2', 'section-3'])
        # Dokument bez nagłówków to jeden rozdział.
        self.assertEqual(len(split_chapters('<p>Jeden</p><p>Dwa</p>', 'Dokument')), 1)

    def test_warnings_are_polish_and_grouped(self):
        warnings = polish_warnings([
            'Unrecognised paragraph style: Cytat (Style ID: Cytat)',
            'Unrecognised paragraph style: Cytat (Style ID: Cytat)',
            'Something new',
        ])
        self.assertEqual(warnings[0], 'Styl akapitu „Cytat” nie ma odpowiednika w e-booku – tekst zachowano '
                                      'w zwykłym formacie. (2 razy)')
        self.assertEqual(warnings[1], 'Uwaga konwertera: Something new')


class ConversionPageTests(IsolatedProgramDirsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('programy', 'Redaktor')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)

    @tag('slow')
    def test_conversion_preserves_name_for_single_and_multiple_formats(self):
        name = 'Żółw i smok.v2.docx'
        for formats, ext in ((['pdf'], 'pdf'), (['pdf', 'epub'], 'zip')):
            with self.subTest(formats=formats):
                response = run_program(self.client, {
                    'program_action': 'convert',
                    'convert-formats': formats,
                    'convert-document': SimpleUploadedFile(name, formatted_docx()),
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn('Żółw i smok.v2.' + ext, unquote(response['Content-Disposition']))
                payload = downloaded(response)
                if ext == 'zip':
                    with ZipFile(BytesIO(payload)) as archive:
                        self.assertEqual(set(archive.namelist()), {'Żółw i smok.v2.pdf', 'Żółw i smok.v2.epub'})
                else:
                    self.assertTrue(payload.startswith(b'%PDF'))
