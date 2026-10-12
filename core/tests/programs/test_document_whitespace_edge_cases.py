"""Zamiana miękkich enterów na akapity: kontrolki zawartości dostają nowe identyfikatory, poziom konspektu
akapitu decyduje o nagłówku, a dokument bez części stylów też jest dzielony."""
from io import BytesIO
from zipfile import ZipFile

from django.test import SimpleTestCase
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from core.services.document_whitespace import normalize_spacing_docx

from .support import content_control, saved

W14 = 'http://schemas.microsoft.com/office/word/2010/wordml'


def body_paragraphs(stream):
    with ZipFile(stream) as archive:
        root = etree.fromstring(archive.read('word/document.xml'))
    return root.find(qn('w:body')).findall(qn('w:p'))


def text(paragraph):
    return ''.join(paragraph.itertext())


class SoftBreakTests(SimpleTestCase):
    def test_content_control_split_by_a_soft_break_gets_a_new_id(self):
        """Kopia kontrolki w nowym akapicie dostaje identyfikator większy od wszystkich w dokumencie – także od
        identyfikatorów innych kontrolek (<w:id w:val>), a nie tylko od atrybutów w:id."""
        document = Document()
        paragraph = document.add_paragraph()
        paragraph._p.append(content_control('<w:r><w:t>Przed</w:t><w:br/><w:t>po</w:t></w:r>'))
        other = content_control('<w:r><w:t>Inna</w:t></w:r>')
        other.find(f'{qn("w:sdtPr")}/{qn("w:id")}').set(qn('w:val'), '1')
        document.add_paragraph()._p.append(other)
        first, second, third = body_paragraphs(normalize_spacing_docx(saved(document)))
        self.assertEqual((text(first), text(second), text(third)), ('Przed', 'po', 'Inna'))
        ids = [p.find(f'{qn("w:sdt")}/{qn("w:sdtPr")}/{qn("w:id")}').get(qn('w:val')) for p in (first, second, third)]
        self.assertEqual((ids[0], ids[2]), ('101', '1'))
        self.assertEqual(ids[1], '102')

    def test_direct_outline_level_decides_whether_the_line_break_stays(self):
        """Poziom konspektu 1–9 to nagłówek (miękki enter zostaje); w:val="9" to zwykły tekst."""
        for level, expected in (('0', ['Tytuł rozdziału']), ('9', ['Tytuł', ' rozdziału'])):
            document = Document()
            paragraph = document.add_paragraph('Tytuł')
            paragraph.add_run().add_break()
            paragraph.add_run(' rozdziału')
            paragraph._p.get_or_add_pPr().append(OxmlElement('w:outlineLvl', {qn('w:val'): level}))
            with self.subTest(level=level):
                self.assertEqual([text(p) for p in body_paragraphs(normalize_spacing_docx(saved(document)))],
                                 expected)

    def test_package_without_styles_and_paragraph_identity(self):
        """Bez word/styles.xml żaden styl nie jest nagłówkiem; druga część traci tylko w14:paraId."""
        document = Document()
        paragraph = document.add_paragraph('Pierwsza')
        paragraph.add_run().add_break()
        paragraph.add_run('druga')
        paragraph._p.set(f'{{{W14}}}paraId', '1A2B3C4D')
        paragraph._p.set(qn('w:rsidR'), '00AB12CD')
        source, stripped = saved(document), BytesIO()
        with ZipFile(source) as archive, ZipFile(stripped, 'w') as target:
            for item in archive.infolist():
                if item.filename != 'word/styles.xml':
                    target.writestr(item, archive.read(item))
        first, second = body_paragraphs(normalize_spacing_docx(stripped))
        self.assertEqual((text(first), text(second)), ('Pierwsza', 'druga'))
        self.assertEqual(first.get(f'{{{W14}}}paraId'), '1A2B3C4D')
        self.assertIsNone(second.get(f'{{{W14}}}paraId'))
        self.assertEqual(second.get(qn('w:rsidR')), '00AB12CD')
