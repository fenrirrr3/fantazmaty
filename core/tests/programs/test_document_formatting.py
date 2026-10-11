"""Przygotowanie dokumentów: dziedziczenie stylów, normalizacja typografii i strony, wyjustowanie, kolejność kroków
przygotowania oraz zamiana miękkich enterów i twardych spacji."""
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase, tag
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.text.paragraph import Paragraph

from core.services.document_conversion_worker import main as conversion_worker_main
from core.services.document_formatting import normalize_docx
from core.services.document_preparation import prepare_docx
from core.services.document_styles import paragraph_properties, paragraph_property
from core.services.document_whitespace import normalize_spacing_docx
from core.services.mailbox_import import package_messages
from core.services.odkurzacz import clean_docx

from .support import IsolatedProgramDirsMixin, saved, typography_sample

PARA_ID = '{http://schemas.microsoft.com/office/word/2010/wordml}paraId'


def justification_sample():
    document = Document()
    paragraph = document.add_paragraph('Zwykły tekst ')
    paragraph.add_run('kursywa').italic = True
    paragraph.add_run(' gruby').bold = True
    document.add_paragraph('Do prawej').alignment = WD_ALIGN_PARAGRAPH.RIGHT
    document.add_paragraph('Pośrodku').alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.styles['Title'].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.add_paragraph('Środek ze stylu', style='Title')
    document.add_paragraph('')
    return saved(document)


class ParagraphStyleTests(SimpleTestCase):
    def test_direct_zero_and_false_override_inherited_values(self):
        document = Document()
        base = document.styles.add_style('Parent', WD_STYLE_TYPE.PARAGRAPH)
        base.paragraph_format.space_after = Pt(18)
        base.paragraph_format.page_break_before = True
        base.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        child = document.styles.add_style('Child', WD_STYLE_TYPE.PARAGRAPH)
        child.base_style = base
        child.paragraph_format.line_spacing = 2
        paragraph = document.add_paragraph('Test', style=child)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.page_break_before = False
        expected = {'space_after': Pt(0), 'page_break_before': False,
                    'alignment': WD_ALIGN_PARAGRAPH.RIGHT, 'line_spacing': 2}
        self.assertEqual(paragraph_properties(paragraph, expected), expected)
        for name, value in expected.items():
            self.assertEqual(paragraph_property(paragraph, name), value)

    def test_missing_properties_in_cyclic_styles_remain_unset(self):
        document = Document()
        one = document.styles.add_style('CycleOne', WD_STYLE_TYPE.PARAGRAPH)
        two = document.styles.add_style('CycleTwo', WD_STYLE_TYPE.PARAGRAPH)
        one.base_style = two
        two.base_style = one
        two.paragraph_format.line_spacing = 2
        paragraph = document.add_paragraph('Test', style=one)
        self.assertEqual(paragraph_properties(paragraph, ('line_spacing', 'page_break_before')),
                         {'line_spacing': 2, 'page_break_before': None})
        self.assertIsNone(paragraph_property(paragraph, 'page_break_before'))

    def test_cyclic_style_chain_is_bounded_during_normalization(self):
        """Regresja: cykl stylów bazowych zapętlał normalizację dokumentu."""
        document = Document()
        a = document.styles.add_style('CycleA', 1)
        b = document.styles.add_style('CycleB', 1)
        a.base_style = b
        b.base_style = a
        document.add_paragraph('Tekst', style=a)
        with normalize_docx(saved(document)) as result:
            self.assertEqual(Document(result).paragraphs[0].text, 'Tekst')


class NormalizationTests(SimpleTestCase):
    def test_typography_preserves_runs_and_inherited_alignment(self):
        with normalize_docx(typography_sample()) as out:
            doc = Document(out)
        first, center, right = doc.paragraphs
        self.assertTrue(first.runs[0].italic)
        self.assertTrue(first.runs[1].bold)
        self.assertEqual(first.runs[0].font.name, 'Times New Roman')
        self.assertEqual(first.runs[0].font.size.pt, 12)
        self.assertEqual(first.paragraph_format.line_spacing, 1.5)
        self.assertEqual(first.paragraph_format.space_after.pt, 0)
        self.assertEqual(first.paragraph_format.space_before.pt, 0)
        self.assertAlmostEqual(first.paragraph_format.first_line_indent.cm, 1.25, places=2)
        self.assertEqual(center.paragraph_format.first_line_indent, 0)
        self.assertEqual(center.style.paragraph_format.alignment, WD_ALIGN_PARAGRAPH.CENTER)
        self.assertEqual(right.alignment, WD_ALIGN_PARAGRAPH.RIGHT)
        self.assertEqual(doc.tables[0].cell(0, 0).paragraphs[0].runs[0].font.size.pt, 12)

    def test_page_normalization_all_sections(self):
        doc = Document()
        doc.add_paragraph('A')
        doc.add_section()
        for section in doc.sections:
            section.page_width = Cm(40)
            section.page_height = Cm(20)
            section.top_margin = section.bottom_margin = Cm(1)
        result = Document(normalize_docx(saved(doc)))
        for section in result.sections:
            self.assertAlmostEqual(section.page_width.cm, 21, places=2)
            self.assertAlmostEqual(section.page_height.cm, 29.7, places=2)
            for name in ('left_margin', 'right_margin', 'top_margin', 'bottom_margin'):
                self.assertAlmostEqual(getattr(section, name).cm, 2.5, places=2)

    def test_formatting_happens_before_cleaning(self):
        observed = []

        def cleaner(source, rules):
            observed.append(Document(source).paragraphs[0].runs[0].font.size.pt)
            source.seek(0)
            return clean_docx(source, rules)

        with (
            TemporaryDirectory() as tmp,
            patch('core.services.document_preparation.clean_docx', side_effect=cleaner),
        ):
            directory = Path(tmp)
            (directory / 'source.docx').write_bytes(typography_sample().getvalue())
            (directory / 'job.json').write_text(
                json.dumps({'formats': ['epub'], 'prepare': True, 'clean': True, 'title': 'Test'}))
            with patch('sys.argv', ['worker', tmp]):
                self.assertEqual(conversion_worker_main(), 0)
        self.assertEqual(observed, [12])


class JustificationTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def check_document(self, stream):
        doc = Document(stream)
        self.assertEqual(len(doc.paragraphs), 5)
        self.assertEqual(doc.paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.JUSTIFY)
        self.assertEqual(doc.paragraphs[1].alignment, WD_ALIGN_PARAGRAPH.JUSTIFY)
        for paragraph in doc.paragraphs[2:4]:
            self.assertEqual(paragraph.paragraph_format.first_line_indent, 0)
            self.assertTrue(
                paragraph.alignment == WD_ALIGN_PARAGRAPH.CENTER
                or paragraph.style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
            )
        self.assertTrue(doc.paragraphs[0].runs[1].italic)
        self.assertTrue(doc.paragraphs[0].runs[2].bold)
        self.assertEqual(doc.paragraphs[4].text, '')

    def test_shared_preparation_only_justifies_when_requested(self):
        with prepare_docx(justification_sample(), normalize_formatting=True, justify=True) as output:
            self.check_document(output)
        with prepare_docx(justification_sample(), normalize_formatting=True) as output:
            self.assertEqual(Document(output).paragraphs[1].alignment, WD_ALIGN_PARAGRAPH.RIGHT)

    @tag('slow')
    def test_mail_conversion_justifies_docx_pdf_and_epub(self):
        row = {'uid': 1, 'folder': 'Tekst', 'files': [('tekst.docx', justification_sample().getvalue())]}
        with package_messages([row], clean=True, convert=True) as result, ZipFile(result) as archive:
            self.check_document(BytesIO(archive.read('Tekst/01_tekst.docx')))
            self.assertTrue(archive.read('Tekst/01_tekst.pdf').startswith(b'%PDF'))
            with ZipFile(BytesIO(archive.read('Tekst/01_tekst.epub'))) as epub:
                content = epub.read('EPUB/content.xhtml').decode()
                self.assertIn('align-justify', content)
                self.assertIn('align-center', content)


class PreparationTests(SimpleTestCase):
    def test_unknown_cleaner_rules_are_refused(self):
        for rules in (['nieznana'], 'spaces'):
            with self.subTest(rules=rules), self.assertRaisesMessage(ValueError, 'Invalid cleaner rules'):
                prepare_docx(typography_sample(), cleaner_rules=rules)

    def test_rebuild_runs_first_and_drops_private_metadata(self):
        document = Document()
        document.core_properties.author = 'Prywatny'
        document.add_paragraph('Ala  ma kota')
        with prepare_docx(saved(document), rebuild=True, cleaner_rules=['spaces']) as output:
            result = Document(output)
        self.assertEqual(result.core_properties.author, '')
        self.assertEqual(result.paragraphs[0].text, 'Ala ma kota')


class SoftWhitespaceTests(SimpleTestCase):
    """Miękki enter (także U+2028) tworzy nowy akapit; właściwości akapitu trafiają tylko tam, gdzie mają sens."""

    def normalized(self, document):
        result = Document(normalize_spacing_docx(saved(document)))
        return [Paragraph(element, result) for element in result.element.body.iter(qn('w:p'))]

    def test_line_separator_splits_paragraph_and_keeps_section_and_page_break_once(self):
        document = Document()
        paragraph = document.add_paragraph('Pierwszy\u2028Drugi\u00a0wiersz')
        paragraph.paragraph_format.page_break_before = True
        paragraph._p.set(PARA_ID, '1234ABCD')
        paragraph._p.get_or_add_pPr().append(OxmlElement('w:sectPr'))
        first, second = self.normalized(document)[:2]
        self.assertEqual((first.text, second.text), ('Pierwszy', 'Drugi wiersz'))
        self.assertTrue(first.paragraph_format.page_break_before)
        self.assertIsNone(second.paragraph_format.page_break_before)
        self.assertIsNone(first._p.pPr.find(qn('w:sectPr')))
        self.assertIsNotNone(second._p.pPr.find(qn('w:sectPr')))
        # Identyfikator akapitu Worda zostaje tylko przy pierwszej części.
        self.assertEqual((first._p.get(PARA_ID), second._p.get(PARA_ID)), ('1234ABCD', None))

    def test_text_box_paragraph_inside_a_split_paragraph_stays_whole(self):
        document = Document()
        outer = document.add_paragraph('Przed')
        outer.add_run().add_break()
        box = OxmlElement('w:txbxContent')
        inner = OxmlElement('w:p')
        run = OxmlElement('w:r')
        text = OxmlElement('w:t')
        text.text = 'Pole tekstowe'
        run.append(text)
        inner.append(run)
        box.append(inner)
        outer.add_run('Po')._r.append(box)
        before, after, boxed = self.normalized(document)
        self.assertEqual([before.text, after.text, boxed.text], ['Przed', 'Po', 'Pole tekstowe'])
        self.assertEqual(len(after._p.xpath('.//w:txbxContent/w:p')), 1)

    def test_oversized_package_is_refused(self):
        stream = BytesIO()
        with ZipFile(stream, 'w') as archive:
            for index in range(2001):
                archive.writestr(f'part{index}.xml', b'')
        with self.assertRaisesMessage(ValueError, 'limit rozpakowanej zawartości'):
            normalize_spacing_docx(stream)
