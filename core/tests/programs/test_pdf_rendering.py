"""Skład PDF: geometria akapitów z DOCX (także w listach i tabelach), podziały stron, obrazy i odrzucanie
nietypowego układu strony."""
import re
from base64 import b64decode
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt
from fpdf import FPDF
from lxml import html as html_parser

from core.services.document_pdf import render_pdf

from .support import png


def pdf_html_spy(captured):
    """Podmiana FPDF.write_html zapisująca przekazany HTML i pozycję kursora."""
    original = FPDF.write_html

    def spy(pdf, content, **kwargs):
        captured.append((content, pdf.y))
        return original(pdf, content, **kwargs)
    return patch.object(FPDF, 'write_html', spy)


class PdfRenderingTests(SimpleTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def render(self, document, content, assets=None):
        """Zwraca (HTML przekazany do fpdf jako jedno drzewo, liczba stron PDF)."""
        source = self.directory / 'source.docx'
        document.save(source)
        captured = []
        with pdf_html_spy(captured):
            render_pdf(source, self.directory / 'result.pdf', content, assets or {}, 'Test')
        pdf = (self.directory / 'result.pdf').read_bytes()
        self.assertTrue(pdf.startswith(b'%PDF'))
        tree = html_parser.fragment_fromstring(''.join(part for part, _ in captured), create_parent='div')
        return tree, len(re.findall(rb'/Type /Page\b(?!s)', pdf)), captured

    def test_pdf_receives_justification_and_empty_lines(self):
        doc = Document()
        doc.add_paragraph('Justowany tekst.').alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _, _, captured = self.render(doc, '<p>Justowany tekst.</p><p></p><p></p><p>Koniec</p>')
        self.assertIn('align="justify"', captured[0][0])
        self.assertGreaterEqual(captured[1][1] - captured[0][1], 18.9)

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
        tree, _, _ = self.render(doc, '<ol><li>Lista</li></ol><table><tr><td><p>Tabela</p></td></tr></table>')
        listed = tree.xpath('.//li/p')[0]
        self.assertEqual(listed.get('align'), 'center')
        self.assertEqual(listed.get('data-indent'), '0')
        self.assertEqual(listed.get('line-height'), '2.0')
        self.assertEqual(tree.xpath('.//td/p')[0].get('align'), 'right')

    def test_style_font_size_spacing_and_page_breaks(self):
        doc = Document()
        doc.styles['Normal'].font.size = Pt(14)
        spaced = doc.add_paragraph('Odstępy')
        spaced.paragraph_format.space_before = Pt(12)
        spaced.paragraph_format.space_after = Pt(6)
        doc.add_paragraph('Nowa strona').paragraph_format.page_break_before = True
        tree, pages, _ = self.render(doc, '<p>Odstępy</p><p>Nowa strona</p>' + '<p></p>' * 60)
        first, second = tree.xpath('./p')[:2]
        self.assertEqual(first.get('data-size'), '14.0')
        self.assertAlmostEqual(float(first.get('data-before')), 4.23, places=2)
        self.assertAlmostEqual(float(first.get('data-after')), 2.12, places=2)
        self.assertEqual(second.get('data-page-break'), '1')
        # Podział przed drugim akapitem i 60 pustych wierszy, które nie mieszczą się na jednej stronie.
        self.assertEqual(pages, 3)

    def test_embedded_images_are_scaled_to_the_text_column(self):
        doc = Document()
        doc.add_paragraph('Tekst')
        tree, _, _ = self.render(doc, '<p>Tekst</p><p><img src="assets/image-1.png"></p>',
                                 {'assets/image-1.png': png(4000, 10).getvalue()})
        image = tree.xpath('.//img')[0]
        self.assertTrue(image.get('src').startswith('data:image/png;base64,'))
        self.assertTrue(b64decode(image.get('src').split(',', 1)[1]).startswith(b'\x89PNG'))
        self.assertLess(float(image.get('width')), 4000 * 0.75)

    def test_unknown_image_and_unusual_pages_are_refused(self):
        doc = Document()
        with self.assertRaisesMessage(ValueError, 'Unknown image'):
            self.render(doc, '<p><img src="https://example.test/x.png"></p>')
        doc.sections[0].page_width = Cm(100)
        with self.assertRaisesMessage(ValueError, 'Unsupported page dimensions'):
            self.render(doc, '<p>A</p>')
        doc = Document()
        doc.sections[0].left_margin = doc.sections[0].right_margin = Cm(10)
        with self.assertRaisesMessage(ValueError, 'Unsupported page margins'):
            self.render(doc, '<p>A</p>')
