"""Ujednolicanie formatowania: nieznane python-docx wyrównanie ze stylu, poziom konspektu wpisany w akapit
i styl akapitu o błędnym typie."""
from django.test import SimpleTestCase
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from core.services.document_formatting import normalize_docx

from .support import saved


def indent(paragraph):
    node = paragraph._p.find(f'{qn("w:pPr")}/{qn("w:ind")}')
    return None if node is None else node.get(qn('w:firstLine'))


def alignment(paragraph):
    node = paragraph._p.find(f'{qn("w:pPr")}/{qn("w:jc")}')
    return None if node is None else node.get(qn('w:val'))


class FormattingGapTests(SimpleTestCase):
    def test_style_alignment_unknown_to_python_docx_is_inherited(self):
        """Styl z w:jc „end” (do prawej) i „start” (do lewej): tylko ten drugi akapit jest justowany."""
        document = Document()
        for value in ('end', 'start'):
            style = document.styles.add_style(f'Styl {value}', WD_STYLE_TYPE.PARAGRAPH)
            style.element.get_or_add_pPr().append(OxmlElement('w:jc', {qn('w:val'): value}))
            document.add_paragraph(value, style=style)
        right, left = Document(normalize_docx(saved(document), justify=True)).paragraphs
        self.assertEqual((alignment(right), indent(right)), (None, '709'))
        self.assertEqual((alignment(left), indent(left)), ('both', '709'))

    def test_outline_level_in_the_paragraph_keeps_the_heading_indent(self):
        document = Document()
        for level in ('0', '9'):
            paragraph = document.add_paragraph(f'Poziom {level}')
            paragraph._p.get_or_add_pPr().append(OxmlElement('w:outlineLvl', {qn('w:val'): level}))
        heading, plain = Document(normalize_docx(saved(document))).paragraphs
        self.assertIsNone(indent(heading))  # nagłówek zachowuje swoje wcięcie
        self.assertEqual(indent(plain), '709')  # poziom 9 to zwykły tekst

    def test_style_of_unknown_type_is_not_a_heading(self):
        """Styl z błędnym w:type (uszkodzony DOCX): zwykły akapit – z własnym wyrównaniem albo bez niego
        (wcześniej akapit bez własnego wyrównania przerywał ujednolicanie błędem)."""
        cases = {'own alignment': (WD_ALIGN_PARAGRAPH.RIGHT, 'right'), 'no own alignment': (None, 'both')}
        for case, (own, expected) in cases.items():
            document = Document()
            style = document.styles.add_style('Uszkodzony', WD_STYLE_TYPE.PARAGRAPH)
            style.element.set(qn('w:type'), 'nieznany')
            paragraph = document.add_paragraph('Tekst')
            paragraph.alignment = own
            paragraph._p.get_or_add_pPr().insert(0, OxmlElement('w:pStyle', {qn('w:val'): 'Uszkodzony'}))
            with self.subTest(case):
                result = Document(normalize_docx(saved(document), justify=True)).paragraphs[0]
                self.assertEqual((alignment(result), indent(result)), (expected, '709'))

    def test_theme_fonts_are_replaced_by_times_new_roman(self):
        document = Document()
        run = document.add_paragraph().add_run('Motyw')
        run._r.get_or_add_rPr().get_or_add_rFonts().set(qn('w:asciiTheme'), 'minorHAnsi')
        fonts = Document(normalize_docx(saved(document))).paragraphs[0].runs[0]._r.rPr.rFonts
        self.assertIsNone(fonts.get(qn('w:asciiTheme')))
        self.assertEqual(fonts.get(qn('w:ascii')), 'Times New Roman')
