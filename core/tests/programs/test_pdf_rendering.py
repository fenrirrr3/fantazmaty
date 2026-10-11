"""Skład PDF: geometria akapitów z DOCX (także w listach i tabelach), podziały stron, obrazy, odrzucanie
nietypowego układu strony, numery stron, nagłówki i stopki, konspekt i separatory scen."""
import json
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

from core.services import document_pdf
from core.services.document_conversion_worker import convert
from core.services.document_pdf import CELL_NOTE, PAGE_FIELD, PAGES_FIELD, render_pdf

from .support import (
    add_bookmark, add_complex_field, add_footnote_reference, add_gallery_page_number, add_internal_link,
    add_page_break, add_simple_field, png, with_footnote,
)


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


class PdfWordStructuresTests(SimpleTestCase):
    """Regresja: przypisy, zakładki, odnośniki wewnętrzne i złożone komórki tabel przerywały cały skład PDF."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def convert_pdf(self, document):
        """Pełna ścieżka DOCX → HTML → PDF; zwraca (tekst przekazany do fpdf, uwagi, HTML)."""
        source = self.directory / 'source.docx'
        if hasattr(document, 'save'):
            document.save(source)
        else:
            source.write_bytes(document.getvalue())
        captured = []
        with pdf_html_spy(captured):
            convert(source, self.directory, ['pdf'], 'Test')
        self.assertTrue((self.directory / 'document.pdf').read_bytes().startswith(b'%PDF'))
        warnings = self.directory / 'warnings.json'
        markup = ''.join(part for part, _ in captured)
        tree = html_parser.fragment_fromstring(markup, create_parent='div')
        return tree.text_content(), json.loads(warnings.read_text()) if warnings.exists() else [], markup

    def test_footnotes_bookmarks_and_internal_links(self):
        doc = Document()
        add_footnote_reference(doc.add_paragraph('Zdanie z przypisem.'))
        add_bookmark(doc.add_paragraph(), '_Toc1', 'Rozdział z zakładką')
        add_internal_link(doc.add_paragraph(), '_Toc1', 'Wróć do rozdziału')
        text, warnings, markup = self.convert_pdf(with_footnote(doc, 'Treść przypisu.'))
        for expected in ('Zdanie z przypisem.[1]', 'Rozdział z zakładką', 'Wróć do rozdziału', 'Treść przypisu.'):
            self.assertIn(expected, text)
        # Strzałka powrotu z przypisu i odnośniki bez celu w PDF nie mają sensu.
        self.assertNotIn('↑', text)
        self.assertNotIn('href="#', markup)

    def test_complex_table_cells_become_plain_text(self):
        doc = Document()
        table = doc.add_table(rows=1, cols=3)
        table.cell(0, 0).text = 'Pierwszy akapit'
        table.cell(0, 0).add_paragraph('Drugi akapit')
        mixed = table.cell(0, 1).paragraphs[0]
        mixed.add_run('Zwykły ')
        mixed.add_run('gruby').bold = True
        inner = table.cell(0, 2).add_table(rows=1, cols=2)
        inner.cell(0, 0).text = 'Wewnątrz A'
        inner.cell(0, 1).text = 'Wewnątrz B'
        doc.add_paragraph('Za tabelą')
        text, warnings, markup = self.convert_pdf(doc)
        for expected in ('Pierwszy akapit\nDrugi akapit', 'Zwykły gruby', 'Wewnątrz A\nWewnątrz B', 'Za tabelą'):
            self.assertIn(expected, text)
        self.assertIn(CELL_NOTE, warnings)

    def test_simple_cells_keep_formatting_without_a_note(self):
        doc = Document()
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).paragraphs[0].add_run('Gruby').bold = True
        table.cell(0, 1).text = 'Zwykły'
        _, warnings, markup = self.convert_pdf(doc)
        self.assertIn('<strong>Gruby</strong>', markup)
        self.assertEqual(warnings, [])

    def test_characters_missing_from_the_font_are_reported(self):
        doc = Document()
        doc.add_paragraph('Zażółć gęślą jaźń „cytat” – … ✓ 中')
        with self.assertLogs('fpdf.output', 'WARNING'):
            _, warnings, _ = self.convert_pdf(doc)
        self.assertEqual(warnings, ['W PDF pominięto znaki, których nie ma w czcionce dokumentu (✓, 中).'])


def page_furniture_spy(records):
    """Podmiana _draw_lines zapisująca (strona, 'nagłówek'|'stopka', [(tekst, wyrównanie)]) każdego wywołania."""
    original = document_pdf._draw_lines

    def spy(pdf, lines, y):
        page = pdf.page_no()
        texts = [('\t'.join(' '.join(part.split()) for part in text.split('\t'))
                  .replace(PAGE_FIELD, str(page)).replace(PAGES_FIELD, '{nb}'), align) for text, align, _ in lines]
        records.append((page, 'nagłówek' if y < pdf.h / 2 else 'stopka', texts))
        return original(pdf, lines, y)
    return patch.object(document_pdf, '_draw_lines', spy)


class PdfPageFurnitureTests(SimpleTestCase):
    """Numery stron, nagłówki i stopki DOCX, konspekt PDF, podziały stron i separatory scen."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def render(self, document, content):
        """Zwraca (wywołania nagłówków i stopek, wywołania start_section, liczba stron, HTML dla fpdf)."""
        source = self.directory / 'source.docx'
        document.save(source)
        records, sections, captured, self.cells = [], [], [], []
        original, cell = FPDF.start_section, FPDF.cell

        def cell_spy(pdf, *args, **kwargs):
            self.cells.append((kwargs.get('text'), kwargs.get('align')))
            return cell(pdf, *args, **kwargs)

        def section_spy(pdf, name, level=0, strict=True):
            sections.append((name, level, pdf.page_no()))
            return original(pdf, name, level, strict)
        with page_furniture_spy(records), pdf_html_spy(captured), \
                patch.object(FPDF, 'start_section', section_spy), patch.object(FPDF, 'cell', cell_spy):
            render_pdf(source, self.directory / 'result.pdf', content, {}, 'Test')
        pdf = (self.directory / 'result.pdf').read_bytes()
        self.pdf = pdf
        return records, sections, len(re.findall(rb'/Type /Page\b(?!s)', pdf)), captured

    def test_pages_are_numbered_centred_from_one_without_a_footer(self):
        records, _, pages, _ = self.render(Document(), '<p>A</p>' + '<p></p>' * 60)
        self.assertEqual(pages, 2)
        self.assertEqual(records, [(1, 'stopka', [('1', 'C')]), (2, 'stopka', [('2', 'C')])])

    def test_docx_header_and_footer_with_page_fields_replace_the_number(self):
        doc = Document()
        header = doc.sections[0].header.paragraphs[0]
        header.text = 'Autor\tTytuł\tstr. '
        add_complex_field(header, 'PAGE \\* MERGEFORMAT', '99')
        footer = doc.sections[0].footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer.add_run('Strona ')
        add_simple_field(footer, 'PAGE', '99')
        footer.add_run(' z ')
        add_complex_field(footer, 'NUMPAGES', '99')
        records, _, pages, _ = self.render(doc, '<p>A</p>' + '<p></p>' * 60)
        self.assertEqual(pages, 2)
        self.assertEqual(records, [
            (1, 'nagłówek', [('Autor\tTytuł\tstr. 1', 'L')]), (1, 'stopka', [('Strona 1 z {nb}', 'R')]),
            (2, 'nagłówek', [('Autor\tTytuł\tstr. 2', 'L')]), (2, 'stopka', [('Strona 2 z {nb}', 'R')]),
        ])
        # Wynik pól zapisany przez Worda (99) nie trafia do PDF, liczba stron jest wstawiana przy zapisie.
        self.assertNotIn(b'{nb}', self.pdf)
        # Tabulatory stylu Nagłówek: kolejne części do lewej, na środek i do prawej.
        self.assertEqual([call for call in self.cells if call[0] in ('Autor', 'Tytuł', 'str. 1')][:3],
                         [('Autor', 'L'), ('Tytuł', 'C'), ('str. 1', 'R')])

    def test_first_and_even_page_variants(self):
        doc = Document()
        section = doc.sections[0]
        section.different_first_page_header_footer = True
        doc.settings.odd_and_even_pages_header_footer = True
        section.header.paragraphs[0].text = 'Nieparzysta'
        section.even_page_header.paragraphs[0].text = 'Parzysta'
        add_gallery_page_number(section.footer)
        records, _, pages, _ = self.render(doc, '<p>A</p>' + '<p></p>' * 100)
        self.assertEqual(pages, 3)
        # Pierwsza strona bez nagłówka i stopki (brak ich definicji), parzysta bez stopki – jak w Wordzie;
        # dokument ma stopkę, więc numer automatyczny się nie pojawia.
        self.assertEqual(records, [(2, 'nagłówek', [('Parzysta', 'L')]),
                                   (3, 'nagłówek', [('Nieparzysta', 'L')]),
                                   (3, 'stopka', [('', 'L'), ('3', 'R')])])

    def test_page_number_in_header_is_not_doubled_and_empty_footer_is_ignored(self):
        doc = Document()
        add_complex_field(doc.sections[0].header.paragraphs[0], 'PAGE', '1')
        doc.sections[0].footer.paragraphs[0].text = '   '
        records, _, _, _ = self.render(doc, '<p>A</p>')
        self.assertEqual(records, [(1, 'nagłówek', [('1', 'L')])])

    def test_tall_header_pushes_the_text_down(self):
        doc = Document()
        header = doc.sections[0].header
        header.paragraphs[0].text = 'Pierwszy wiersz'
        for number in range(8):
            header.add_paragraph(f'Wiersz {number}')
        _, _, _, captured = self.render(doc, '<p>Tekst</p>')
        # 9 wierszy po 12 pt (×1,15) od 12,7 mm: tekst zaczyna się niżej niż margines 25,4 mm.
        self.assertGreater(captured[0][1], 12.7 + 9 * 12 * 1.15 / (72 / 25.4) - 0.01)

    def test_word_headings_become_the_pdf_outline(self):
        doc = Document()
        content = ('<h1>Tytuł <em>z kursywą</em></h1><p>A</p><h3>Głęboko</h3><h2>Rozdział</h2><h4>Pominięty</h4>'
                   '<p>B</p><h1></h1>')
        _, sections, _, _ = self.render(doc, content)
        self.assertEqual(sections, [('Tytuł z kursywą', 0, 1), ('Głęboko', 1, 1), ('Rozdział', 1, 1)])
        self.assertIn(b'/Outlines', self.pdf)

    def test_heading_starting_on_the_next_page_is_bookmarked_there(self):
        _, sections, pages, _ = self.render(Document(), '<p>A</p>' + '<p></p>' * 44 + '<h1>Na dole</h1>')
        self.assertEqual((pages, sections), (2, [('Na dole', 0, 2)]))

    def test_page_breaks_split_paragraphs_without_blank_pages(self):
        doc = Document()
        add_page_break(doc.add_paragraph('Przed'), 'Po')
        add_page_break(doc.add_paragraph())
        doc.add_paragraph('Dalej')
        add_page_break(doc.add_paragraph('Koniec'))
        content = ('<p>Przed<br class="page-break">Po</p><p><br class="page-break"></p><p>Dalej</p>'
                   '<p>Koniec<br class="page-break"></p>')
        records, _, pages, captured = self.render(doc, content)
        # Strony: „Przed” | „Po” | „Dalej Koniec”; akapit z samym podziałem i podział na końcu nie dodają stron.
        self.assertEqual(pages, 3)
        texts = [html_parser.fragment_fromstring(part).text_content() for part, _ in captured]
        self.assertEqual(texts, ['Przed', 'Po', 'Dalej', 'Koniec'])
        self.assertNotIn('<br', ''.join(part for part, _ in captured))
        self.assertEqual([page for page, _, _ in records], [1, 2, 3])

    def test_scene_breaks_are_centred_with_space(self):
        doc = Document()
        doc.add_paragraph('Scena pierwsza.')
        doc.add_paragraph('* * *')
        doc.add_paragraph('Scena druga.')
        _, _, _, captured = self.render(doc, '<p>Scena pierwsza.</p><p>* * *</p><p>Scena druga.</p>'
                                             '<ul><li>*</li></ul>')
        separator = html_parser.fragment_fromstring(captured[1][0])
        self.assertEqual((separator.get('align'), separator.get('data-indent')), ('center', '0'))
        self.assertGreater(float(separator.get('data-before')), 6)
        self.assertGreater(float(separator.get('data-after')), 6)
        self.assertNotIn('align="center"', captured[3][0])  # punkt listy to nie separator
