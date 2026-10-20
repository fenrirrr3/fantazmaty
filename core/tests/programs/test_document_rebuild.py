"""Przebudowa dokumentu do nowego DOCX: zachowana treść i formatowanie, usunięte metadane, ostrzeżenia o pominięciach."""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor
from lxml import etree

from core.program_test_support import run_program
from core.services import program_jobs as jobs
from core.services.document_converter import RebuildConfirmationRequired, convert_document
from core.services.document_preparation import DEFAULT_EDITORIAL_RULES, prepare_docx
from core.services.document_rebuild import PLACEHOLDER_OMISSION, RebuildUnsupported, inspect_docx, rebuild_docx
from core.services.mailbox_import import package_messages
from fantazmaty.testing import create_superuser

from .support import (
    NOTE_PARTS, IsolatedProgramDirsMixin, add_block, add_complex_field, add_text_box, content_control,
    downloaded_document, noted_document, noted_text, saved, text_run,
)

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


class RebuildTests(SimpleTestCase):
    def test_text_format_empty_paragraphs_and_metadata(self):
        doc = Document()
        doc.core_properties.author = 'Secret'
        doc.core_properties.title = 'Private'
        doc.sections[0].header.paragraphs[0].text = 'Private header'
        doc.styles['Normal'].font.italic = True
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Cm(1.25)
        p.add_run('Tekst').bold = True
        doc.add_paragraph()
        doc.add_paragraph()
        doc.add_paragraph('Środek').alignment = WD_ALIGN_PARAGRAPH.CENTER
        with rebuild_docx(saved(doc), allow_omissions=True) as out:
            result = Document(out)
            self.assertEqual([p.text for p in result.paragraphs], ['Tekst', '', '', 'Środek'])
            self.assertTrue(result.paragraphs[0].runs[0].bold)
            self.assertTrue(result.paragraphs[0].runs[0].italic)
            self.assertEqual(result.paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.JUSTIFY)
            self.assertEqual(result.paragraphs[-1].alignment, WD_ALIGN_PARAGRAPH.CENTER)
            self.assertEqual(result.core_properties.author, '')
            self.assertIsNone(result.core_properties.created)
            with ZipFile(out) as archive:
                self.assertNotIn('word/header1.xml', archive.namelist())
                self.assertNotIn(b'Secret', archive.read('docProps/core.xml'))
            self.assertAlmostEqual(result.sections[0].page_width.cm, 21, places=2)
            self.assertAlmostEqual(result.sections[0].left_margin.cm, 2.5, places=2)

    def test_doc_defaults_and_theme_fonts_are_kept(self):
        doc = Document()
        doc.add_paragraph('Test')
        defaults = doc.styles.element.find(qn('w:docDefaults')).find(qn('w:rPrDefault')).find(qn('w:rPr'))
        defaults.find(qn('w:sz')).set(qn('w:val'), '34')
        font = defaults.find(qn('w:rFonts'))
        for key in list(font.attrib):
            del font.attrib[key]
        font.set(qn('w:ascii'), 'Courier New')
        font.set(qn('w:hAnsi'), 'Courier New')
        rebuilt = Document(rebuild_docx(saved(doc)))
        self.assertEqual(rebuilt.paragraphs[0].runs[0].font.size.pt, 17)
        self.assertEqual(rebuilt.paragraphs[0].runs[0].font.name, 'Courier New')

    def test_revisions_and_hidden_text(self):
        doc = Document()
        p = doc.add_paragraph('A')
        p.add_run('hidden').font.hidden = True
        for tag_name, text in [('ins', 'B'), ('del', 'C')]:
            wrapper = OxmlElement('w:' + tag_name)
            run = OxmlElement('w:r')
            t = OxmlElement('w:t' if tag_name == 'ins' else 'w:delText')
            t.text = text
            run.append(t)
            wrapper.append(run)
            p._p.append(wrapper)
        self.assertEqual(Document(rebuild_docx(saved(doc))).paragraphs[0].text, 'AB')

    def test_tables_and_numbering_fail_explicitly(self):
        for kind in ('table', 'list'):
            doc = Document()
            if kind == 'table':
                doc.add_table(rows=1, cols=1)
            else:
                doc.add_paragraph('item', 'List Number')
            with self.assertRaises(RebuildUnsupported):
                rebuild_docx(saved(doc))

    def test_nonempty_header_requires_rebuild_acceptance(self):
        document = Document()
        document.add_paragraph('Treść')
        document.sections[0].header.paragraphs[0].text = 'Ważny nagłówek'
        with self.assertRaises(RebuildUnsupported) as error:
            inspect_docx(saved(document))
        self.assertIn('nagłówki', str(error.exception))
        with rebuild_docx(saved(document), allow_omissions=True) as result:
            self.assertEqual(Document(result).paragraphs[0].text, 'Treść')

    def test_loss_warning_lists_sections_and_links_and_keeps_headings(self):
        doc = Document()
        doc.add_heading('Rozdział', 1)
        doc.add_section()
        link = OxmlElement('w:hyperlink')
        run = OxmlElement('w:r')
        t = OxmlElement('w:t')
        t.text = 'Odnośnik'
        run.append(t)
        link.append(run)
        doc.add_paragraph()._p.append(link)
        with self.assertRaises(RebuildUnsupported) as error:
            rebuild_docx(saved(doc))
        self.assertIn('sekcje', str(error.exception))
        self.assertIn('hiperłączy', str(error.exception))
        self.assertIn('liczba: 1', str(error.exception))
        rebuilt = Document(rebuild_docx(saved(doc), allow_omissions=True))
        self.assertEqual(rebuilt.paragraphs[0].style.name, 'Heading 1')


class RebuildPageLayoutWarningTests(SimpleTestCase):
    """Przebudowa ustawia A4 – inny format książki, orientacja lub kolumny wymagają potwierdzenia."""

    def test_book_format_landscape_and_columns_are_reported(self):
        for name, change in (
            ('A5', lambda section: (setattr(section, 'page_width', Cm(14.8)),
                                    setattr(section, 'page_height', Cm(21)))),
            ('poziomo', lambda section: (setattr(section, 'page_width', Cm(29.7)),
                                         setattr(section, 'page_height', Cm(21)))),
            ('kolumny', lambda section: section._sectPr.find(qn('w:cols')).set(qn('w:num'), '2')),
        ):
            with self.subTest(name):
                document = Document()
                document.add_paragraph('Treść')
                change(document.sections[0])
                with self.assertRaises(RebuildUnsupported) as error:
                    inspect_docx(saved(document))
                self.assertIn('format strony, orientacja lub kolumny', str(error.exception))
                rebuilt = Document(rebuild_docx(saved(document), allow_omissions=True))
                self.assertAlmostEqual(rebuilt.sections[0].page_width.cm, 21, places=1)

    def test_a4_and_letter_with_any_margins_need_no_confirmation(self):
        for width, height in ((21, 29.7), (21.59, 27.94)):
            with self.subTest(width=width):
                document = Document()
                document.add_paragraph('Treść')
                section = document.sections[0]
                section.page_width, section.page_height = Cm(width), Cm(height)
                section.left_margin = section.right_margin = Cm(2)
                self.assertIsNone(inspect_docx(saved(document)))  # bez RebuildUnsupported
                # Przebudowa bez zgody na pominięcia przechodzi i zachowuje treść; wynik ma format A4.
                rebuilt = Document(rebuild_docx(saved(document)))
                self.assertEqual([paragraph.text for paragraph in rebuilt.paragraphs], ['Treść'])
                self.assertAlmostEqual(rebuilt.sections[0].page_width.cm, 21, places=1)


class RebuildDetailTests(SimpleTestCase):
    """Elementy wewnątrz akapitów: zachowane (tabulatory, podziały, łączniki, kolory motywu) albo zgłoszone."""

    @staticmethod
    def element(tag, **attributes):
        node = OxmlElement(tag)
        for key, value in attributes.items():
            node.set(qn(key), value)
        return node

    def run_with(self, paragraph, text, *properties):
        run = paragraph.add_run(text)
        for node in properties:
            run._r.get_or_add_rPr().append(node)
        return run

    def detailed_document(self):
        element = self.element
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.tab_stops.add_tab_stop(Cm(5))
        run = paragraph.add_run('A')
        run.add_tab()
        run.add_text('B')
        run.add_break(WD_BREAK.PAGE)
        run.add_break()
        run.add_text('C')
        for node in (element('w:noBreakHyphen'), element('w:softHyphen'), element('w:commentReference', **{'w:id': '1'}),
                     element('w:ptab')):
            run._r.append(node)
        self.run_with(paragraph, 'ciemny', element('w:color', **{'w:val': 'FF0000', 'w:themeShade': '80'}))
        self.run_with(paragraph, 'jasny', element('w:color', **{'w:val': '000000', 'w:themeTint': '80'}))
        self.run_with(paragraph, 'auto', element('w:color', **{'w:val': 'auto'}))
        self.run_with(paragraph, 'motyw', element('w:rFonts', **{'w:asciiTheme': 'majorHAnsi'}))
        self.run_with(paragraph, 'rozstrzelony', element('w:spacing', **{'w:val': '20'}))
        framed = document.add_paragraph('Ramka')
        properties = framed._p.get_or_add_pPr()
        properties.append(element('w:pBdr'))
        mark = element('w:rPr')
        mark.append(element('w:del', **{'w:id': '2', 'w:author': 'Ktoś'}))
        mark.append(element('w:ins', **{'w:id': '3', 'w:author': 'Ktoś'}))
        properties.append(mark)
        document.element.body.insert(0, element('w:bookmarkStart', **{'w:id': '9', 'w:name': 'zakładka'}))
        return document

    def test_inline_content_is_rebuilt_and_unsupported_parts_are_listed(self):
        document = self.detailed_document()
        with self.assertRaises(RebuildUnsupported) as caught:
            rebuild_docx(saved(document))
        self.assertEqual(caught.exception.omissions, [
            'element ptab wewnątrz tekstu (liczba: 1)',
            'obramowania akapitów (liczba: 1)',
            'oznaczenia usunięcia akapitu (akapit pozostanie) (liczba: 1)',
            'oznaczenia zmian akapitów (podział na akapity pozostanie) (liczba: 1)',
            'rozstrzelenie znaków (liczba: 1)',
        ])
        result = Document(rebuild_docx(saved(document), allow_omissions=True))
        paragraph = result.paragraphs[0]
        self.assertEqual(paragraph.text, 'A\tB\nC\u2011\u00adciemnyjasnyautomotywrozstrzelony')
        self.assertEqual(paragraph._p.xpath('.//w:br/@w:type'), ['page'])
        self.assertAlmostEqual(paragraph.paragraph_format.tab_stops[0].position.cm, 5, places=2)
        runs = {run.text: run for run in paragraph.runs}
        self.assertEqual(runs['ciemny'].font.color.rgb, RGBColor(0x80, 0, 0))
        self.assertEqual(runs['jasny'].font.color.rgb, RGBColor(0x7F, 0x7F, 0x7F))
        self.assertIsNone(runs['auto'].font.color.rgb)
        self.assertNotEqual(runs['motyw'].font.name, runs['auto'].font.name)
        self.assertEqual(result.paragraphs[1].text, 'Ramka')

    def test_theme_fonts_without_a_theme_part_are_reported(self):
        document = Document()
        document.add_paragraph('Tekst')
        for key, relationship in list(document.part.rels.items()):
            if relationship.reltype.endswith('/theme'):
                document.part.drop_rel(key)
        with self.assertRaisesMessage(RebuildUnsupported, 'nierozpoznany krój czcionki z motywu'):
            inspect_docx(saved(document))

    def test_missing_theme_font_is_reported_once_per_document(self):
        """Regresja: brak czcionki motywu to jedna uwaga, nie jedna na każdy fragment tekstu."""
        document = Document()
        for _ in range(3):
            document.add_paragraph('Tekst').add_run(' dalej')
        for key, relationship in list(document.part.rels.items()):
            if relationship.reltype.endswith('/theme'):
                document.part.drop_rel(key)
        with self.assertRaises(RebuildUnsupported) as caught:
            inspect_docx(saved(document))
        self.assertEqual(caught.exception.omissions,
                         ['nierozpoznany krój czcionki z motywu (zastosujemy domyślny) (liczba: 1)'])

    def test_one_equation_is_counted_once(self):
        """Regresja: części równania (m:r, m:t) nie są liczone jako osobne równania."""
        document = Document()
        document.add_paragraph('Wzór')._p.append(
            parse_xml('<m:oMath %s><m:r><m:t>x</m:t></m:r></m:oMath>' % nsdecls('m')))
        with self.assertRaisesMessage(RebuildUnsupported, 'równania matematyczne (liczba: 1)'):
            inspect_docx(saved(document))


def omissions_of(document):
    """Lista ostrzeżeń przebudowy (pusta, gdy nic nie zostanie pominięte)."""
    try:
        inspect_docx(saved(document))
    except RebuildUnsupported as error:
        return error.omissions
    return []


def drop_doc_defaults(document):
    """Dokument bez ustawień domyślnych (jak z LibreOffice lub starszego Worda): Word stosuje własne wartości bazowe."""
    defaults = document.styles.element.find(qn('w:docDefaults'))
    for child in list(defaults):
        defaults.remove(child)


class RebuildFidelityTests(SimpleTestCase):
    """Nowy DOCX nie może wnosić formatowania z szablonu python-docx, którego nie było w źródle."""

    def test_template_defaults_do_not_add_spacing_size_or_font(self):
        document = Document()
        drop_doc_defaults(document)
        document.add_paragraph('Akapit bez własnych ustawień.')
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual(rebuilt.paragraphs[0].text, 'Akapit bez własnych ustawień.')
        styles = rebuilt.styles.element
        # Szablon python-docx ma 10 pt po akapicie, interlinię 1,15, 11 pt i czcionkę motywu.
        for path in ('w:docDefaults//w:spacing', 'w:docDefaults//w:sz', 'w:docDefaults//w:rFonts',
                     'w:style[@w:styleId="Normal"]/w:pPr', 'w:style[@w:styleId="Normal"]/w:rPr'):
            self.assertEqual(styles.xpath(path), [], path)
        # Brak ustawień domyślnych też jest odtworzony: LibreOffice przyjmuje wtedy wartości Worda 2007.
        self.assertEqual(len(styles.xpath('w:docDefaults')), 1)
        self.assertEqual(styles.xpath('w:docDefaults/*'), [])
        run = rebuilt.paragraphs[0].runs[0]
        self.assertIsNone(run.font.size)
        self.assertIsNone(rebuilt.paragraphs[0].paragraph_format.space_after)

    def test_heading_style_keeps_structure_without_template_look(self):
        document = Document()
        heading = document.styles['Heading 1']
        heading.font.color.rgb = None
        heading.paragraph_format.space_before = None
        document.add_heading('Rozdział pierwszy', 1)
        rebuilt = Document(rebuild_docx(saved(document)))
        paragraph = rebuilt.paragraphs[0]
        self.assertEqual(paragraph.style.name, 'Heading 1')
        self.assertIsNone(paragraph.runs[0].font.color.rgb)
        self.assertIsNone(paragraph.style.font.color.rgb)
        self.assertIsNone(paragraph.style.paragraph_format.space_before)
        self.assertIsNone(paragraph.paragraph_format.space_before)
        # Poziom konspektu zostaje: nawigacja i spis treści w Wordzie dalej widzą rozdział.
        self.assertEqual(paragraph.style.element.xpath('w:pPr/w:outlineLvl/@w:val'), ['0'])
        self.assertTrue(paragraph.runs[0].font.bold)
        self.assertTrue(paragraph.paragraph_format.keep_with_next)

    def test_document_and_run_language_are_kept(self):
        document = Document()
        language = document.styles.element.xpath('w:docDefaults//w:lang')[0]
        language.set(qn('w:val'), 'pl-PL')
        paragraph = document.add_paragraph('Zażółć gęślą jaźń. ')
        quote = paragraph.add_run('To be or not to be.')
        quote._r.get_or_add_rPr().append(RebuildDetailTests.element('w:lang', **{'w:val': 'en-GB'}))
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual(rebuilt.styles.element.xpath('w:docDefaults//w:lang/@w:val'), ['pl-PL'])
        polish, english = rebuilt.paragraphs[0].runs
        self.assertEqual(polish._r.xpath('w:rPr/w:lang/@w:val'), [])
        self.assertEqual(english._r.xpath('w:rPr/w:lang/@w:val'), ['en-GB'])

    def test_empty_paragraph_keeps_its_line_height(self):
        """Puste akapity (odstępy między scenami) mają wysokość z formatowania znaku akapitu."""
        document = Document()
        document.styles['Normal'].font.size = Pt(14)
        document.styles['Normal'].font.name = 'Georgia'
        document.add_paragraph('Scena pierwsza.')
        document.add_paragraph()
        rebuilt = Document(rebuild_docx(saved(document)))
        mark = rebuilt.paragraphs[1]._p.xpath('w:pPr/w:rPr')
        self.assertEqual(len(mark), 1)
        self.assertEqual(mark[0].xpath('w:sz/@w:val'), ['28'])
        self.assertEqual(mark[0].xpath('w:rFonts/@w:ascii'), ['Georgia'])

    def test_default_tab_stop_is_kept(self):
        document = Document()
        document.settings.element.find(qn('w:defaultTabStop')).set(qn('w:val'), '709')
        document.add_paragraph('A\tB')
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual(rebuilt.settings.element.xpath('w:defaultTabStop/@w:val'), ['709'])

    def test_invalid_theme_shade_does_not_stop_the_rebuild(self):
        document = Document()
        run = document.add_paragraph().add_run('Czerwony')
        run._r.get_or_add_rPr().append(RebuildDetailTests.element(
            'w:color', **{'w:val': 'FF0000', 'w:themeShade': 'zz', 'w:themeTint': ''}))
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual(rebuilt.paragraphs[0].runs[0].font.color.rgb, RGBColor(0xFF, 0, 0))


class RebuildWarningAccuracyTests(SimpleTestCase):
    """Ostrzeżenia liczą to, co użytkownik widzi w dokumencie: bez podwójnego liczenia i bez fałszywych alarmów."""

    def test_word_text_box_is_one_item_and_surrounding_text_stays(self):
        document = Document()
        add_text_box(document.add_paragraph('Przed polem. '), 'W ramce')
        self.assertEqual(omissions_of(document), ['ilustracje i pola tekstowe wraz z ich treścią (liczba: 1)'])
        rebuilt = Document(rebuild_docx(saved(document), allow_omissions=True))
        self.assertEqual(rebuilt.paragraphs[0].text, 'Przed polem. ')

    def test_alternate_content_without_fallback_keeps_plain_text(self):
        document = Document()
        paragraph = document.add_paragraph('Początek ')
        paragraph._p.append(parse_xml(
            '<mc:AlternateContent %s xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            '<mc:Choice Requires="w14"><w:r><w:t>środek</w:t></w:r></mc:Choice></mc:AlternateContent>'
            % nsdecls('w')))
        paragraph.add_run(' koniec')
        self.assertEqual(omissions_of(document), [])
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual(rebuilt.paragraphs[0].text, 'Początek środek koniec')

    def test_one_field_is_one_item_and_its_result_stays(self):
        document = Document()
        add_complex_field(document.add_paragraph('Strona '), 'PAGE', '7')
        self.assertEqual(omissions_of(document),
                         ['mechanizm pól automatycznych (zwykły tekst wyniku może pozostać) (liczba: 1)'])
        rebuilt = Document(rebuild_docx(saved(document), allow_omissions=True))
        self.assertEqual(rebuilt.paragraphs[0].text, 'Strona 7')

    def test_nested_table_and_image_in_text_box_count_once(self):
        document = Document()
        document.add_table(rows=1, cols=1).cell(0, 0).add_table(rows=1, cols=1)
        self.assertEqual(omissions_of(document), ['tabele wraz z ich treścią (liczba: 1)'])

    def test_content_free_markers_between_paragraphs_are_not_reported(self):
        document = Document()
        document.add_paragraph('A')
        body = document.element.body
        for xml in ('<w:bookmarkStart %s w:id="0" w:name="_GoBack"/>', '<w:bookmarkEnd %s w:id="0"/>',
                    '<w:proofErr %s w:type="gramEnd"/>', '<w:commentRangeEnd %s w:id="4"/>',
                    '<w:permEnd %s w:id="1"/>'):
            body.insert(1, parse_xml(xml % nsdecls('w')))
        document.add_paragraph('B')
        self.assertEqual(omissions_of(document), [])
        self.assertEqual([p.text for p in Document(rebuild_docx(saved(document))).paragraphs], ['A', 'B'])

    def test_numbering_switched_off_is_not_reported(self):
        document = Document()
        paragraph = document.add_paragraph('Zwykły akapit', 'List Number')
        paragraph._p.get_or_add_pPr().append(parse_xml(
            '<w:numPr %s><w:ilvl w:val="0"/><w:numId w:val="0"/></w:numPr>' % nsdecls('w')))
        self.assertEqual(omissions_of(document), [])
        numbered = Document()
        numbered.add_paragraph('Pozycja', 'List Number')
        self.assertEqual(omissions_of(numbered),
                         ['automatyczne numerowanie i punktory list (tekst pozycji pozostanie) (liczba: 1)'])

    def test_formatting_history_of_tracked_changes_is_not_reported(self):
        document = Document()
        paragraph = document.add_paragraph()
        run = paragraph.add_run('Tekst')
        run.bold = True
        run._r.get_or_add_rPr().append(parse_xml(
            '<w:rPrChange %s w:id="5" w:author="Redaktor"><w:rPr><w:spacing w:val="20"/>'
            '<w:shd w:fill="FF0000"/></w:rPr></w:rPrChange>' % nsdecls('w')))
        paragraph._p.get_or_add_pPr().append(parse_xml(
            '<w:pPrChange %s w:id="6" w:author="Redaktor"><w:pPr><w:pBdr/></w:pPr></w:pPrChange>' % nsdecls('w')))
        self.assertEqual(omissions_of(document), [])
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertTrue(rebuilt.paragraphs[0].runs[0].bold)
        self.assertEqual(rebuilt.element.body.xpath('.//w:rPrChange | .//w:pPrChange'), [])

    def test_link_inside_removed_table_is_not_promised_to_stay(self):
        """„Widoczny tekst pozostanie” nie może dotyczyć odnośnika, który znika razem z tabelą."""
        document = Document()
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.paragraphs[0]._p.append(parse_xml(
            '<w:hyperlink %s w:anchor="x"><w:r><w:t>link</w:t></w:r></w:hyperlink>' % nsdecls('w')))
        self.assertEqual(omissions_of(document), ['tabele wraz z ich treścią (liczba: 1)'])

    def test_header_with_only_a_vml_watermark_is_reported(self):
        document = Document()
        document.add_paragraph('Treść')
        header = document.sections[0].header.paragraphs[0]
        header._p.append(parse_xml(
            '<w:r %s xmlns:v="urn:schemas-microsoft-com:vml"><w:pict><v:shape>'
            '<v:textpath string="POUFNE"/></v:shape></w:pict></w:r>' % nsdecls('w')))
        self.assertEqual(omissions_of(document), ['niepuste nagłówki lub stopki (liczba: 1)'])


def note_parts(stream, kind):
    """(tekst treści z odsyłaczami, teksty kolejnych przypisów, korzeń części przypisów albo None, pliki pakietu)."""
    with ZipFile(stream) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    notes = files.get(f'word/{kind}s.xml')
    root = etree.fromstring(notes) if notes is not None else None
    texts = [noted_text(note) for note in root.findall(qn('w:' + kind)) if note.get(qn('w:type')) is None] if notes else []
    return noted_text(etree.fromstring(files['word/document.xml'])), texts, root, files


class RebuildNotesTests(SimpleTestCase):
    """Przypisy dolne i końcowe: odsyłacz na swoim miejscu, treść w nowej części, sanityzowana jak treść główna."""

    def test_notes_are_recreated_in_a_new_part(self):
        """Nowa część z separatorami (w:id -1 i 0), relacją, typem zawartości, ustawieniami i stylami;
        odsyłacz we własnym przebiegu ze stylem odsyłacza, znak przypisu na początku treści, bez XML źródła."""
        for kind, (name, reltype) in NOTE_PARTS.items():
            title = kind.capitalize()
            with self.subTest(kind):
                source = noted_document(['Zdanie', 1, ' dalej.'], kind, note_runs=(
                    '<w:r w:rsidR="00ABCDEF"><w:t xml:space="preserve"> Treść przypisu.</w:t></w:r>'))
                inspect_docx(source)
                result = rebuild_docx(source)
                body, texts, root, files = note_parts(result, kind)
                self.assertEqual(body, 'Zdanie[1] dalej.')
                self.assertEqual(texts, ['[ref] Treść przypisu.'])
                self.assertNotIn(b'00ABCDEF', files[f'word/{name}.xml'])
                self.assertEqual([(note.get(qn('w:type')), note.get(qn('w:id'))) for note in root],
                                 [('separator', '-1'), ('continuationSeparator', '0'), (None, '1')])
                document = Document(result)
                reference = next(document.element.body.iter(qn(f'w:{kind}Reference')))
                self.assertEqual([child.tag for child in reference.getparent()], [qn('w:rPr'), reference.tag])
                self.assertEqual(reference.getparent().style, f'{title}Reference')
                self.assertEqual(root.xpath('./w:*[@w:id="1"]/w:p/w:pPr/w:pStyle/@w:val', namespaces=NS),
                                 [f'{title}Text'])
                styles = document.styles.element
                self.assertEqual(styles.get_by_id(f'{title}Text').type, WD_STYLE_TYPE.PARAGRAPH)
                self.assertEqual(styles.get_by_id(f'{title}Reference').xpath('./w:rPr/w:vertAlign/@w:val'),
                                 ['superscript'])
                self.assertEqual(document.settings.element.xpath(f'./w:{kind}Pr/w:{kind}/@w:id'), ['-1', '0'])
                self.assertIn(f'PartName="/word/{name}.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                              f'wordprocessingml.{name}+xml"'.encode(), files['[Content_Types].xml'])
                self.assertIn(f'Type="{reltype}"'.encode(), files['word/_rels/document.xml.rels'])
                other = 'endnote' if kind == 'footnote' else 'footnote'
                self.assertNotIn(f'word/{other}s.xml', files)

    def test_note_content_follows_the_rules_of_the_main_text(self):
        """Formatowanie, pominięcia (liczone razem z treścią), kolejność i numeracja, brakujący przypis,
        przypis bez odsyłacza i własny znak odsyłacza."""
        drawing = '<w:r><w:t>Tekst</w:t></w:r><w:r><w:drawing/></w:r>'
        table = ('<w:r><w:t>Tekst</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>komórka</w:t></w:r></w:p>'
                 '</w:tc></w:tr></w:tbl><w:p>')
        # nazwa: (przebiegi, opcje noted_document, pominięcia, treść, przypisy, XPath w części przypisów)
        cases = {
            'kursywa': (['A', 1], {'note_runs': '<w:r><w:rPr><w:i/></w:rPr><w:t>kursywa</w:t></w:r>'}, [],
                        'A[1]', ['[ref]kursywa'], './/w:r[w:t="kursywa"]/w:rPr/w:i'),
            'ilustracje w dwóch przypisach': (
                ['A', 1, 'B', 2], {'note_runs': drawing},
                ['ilustracje i pola tekstowe wraz z ich treścią (liczba: 2)'], 'A[1]B[2]', ['[ref]Tekst'] * 2, None),
            'tabela w przypisie': (['A', 1], {'note_runs': table}, ['tabele wraz z ich treścią (liczba: 1)'],
                                   'A[1]', ['[ref]Tekst'], None),
            'kolejność i numeracja': (['A', 3, 'B', 1, 'C', 2], {'note_text': 'Przypis {id}'}, [], 'A[1]B[2]C[3]',
                                      ['[ref]Przypis 3', '[ref]Przypis 1', '[ref]Przypis 2'], None),
            'odsyłacz do brakującego przypisu': (
                ['A', 1, 2], {'notes': [1], 'note_text': 'Przypis {id}'},
                ['odsyłacze do nieistniejących przypisów (liczba: 1)'], 'A[1]', ['[ref]Przypis 1'], None),
            'przypis bez odsyłacza': (['A', 2], {'notes': [1, 2, 3], 'note_text': 'Przypis {id}'}, [], 'A[1]',
                                      ['[ref]Przypis 2'], None),
            'własny znak bez znaku przypisu w treści': (
                ['A', 1, '*'], {'reference': 'w:customMarkFollows="1"', 'note_text': '* Gwiazdka', 'mark': False},
                [], 'A[1]*', ['* Gwiazdka'], './/w:*[@w:id="1"]'),
            'hiperłącze w przypisie': (
                ['A', 1], {'note_runs': '<w:hyperlink><w:r><w:t>link</w:t></w:r></w:hyperlink>'},
                ['odnośniki hiperłączy (widoczny tekst pozostanie) (liczba: 1)'], 'A[1]', ['[ref]link'], None),
            'dwa odsyłacze do tego samego przypisu': (['A', 1, 'B', 1], {'note_text': 'Jeden'}, [], 'A[1]B[2]',
                                                      ['[ref]Jeden', '[ref]Jeden'], None),
            'znak przypisu dopiero po tekście': (
                ['A', 1], {'mark': False, 'note_runs': '<w:r><w:t>x</w:t><w:footnoteRef/></w:r>'},
                [], 'A[1]', ['[ref]x'], None),
        }
        for label, (parts, options, omissions, body, notes, xpath) in cases.items():
            with self.subTest(label):
                source = noted_document(parts, **options)
                try:
                    inspect_docx(source)
                    found = []
                except RebuildUnsupported as error:
                    found = error.omissions
                self.assertEqual(found, omissions)
                result = rebuild_docx(source, allow_omissions=True)
                text, texts, root, files = note_parts(result, 'footnote')
                self.assertEqual((text, texts), (body, notes))
                if xpath:
                    self.assertTrue(root.xpath(xpath, namespaces=NS))
                custom = Document(result).element.body.xpath('.//w:footnoteReference/@w:customMarkFollows')
                self.assertEqual(custom, ['1'] if 'reference' in options else [])

    def test_text_in_the_reference_run_and_rebuilt_separators(self):
        """Tekst w tym samym przebiegu przed odsyłaczem i za nim trafia do osobnych przebiegów; ponowna przebudowa
        wyniku (część z separatorami w:type) odtwarza tylko zwykłe przypisy."""
        source = noted_document(['A', 1], note_text='Przypis')
        output = BytesIO()
        with ZipFile(source) as archive, ZipFile(output, 'w') as target:
            for item in archive.infolist():
                data = archive.read(item.filename)
                if item.filename == 'word/document.xml':
                    root = etree.fromstring(data)
                    reference = root.xpath('.//w:footnoteReference', namespaces=NS)[0]
                    reference.addprevious(parse_xml(f'<w:t {nsdecls("w")}>przed</w:t>'))
                    reference.getparent().append(parse_xml(f'<w:t {nsdecls("w")}>po</w:t>'))
                    data = etree.tostring(root)
                target.writestr(item, data)
        output.seek(0)
        first = rebuild_docx(output)
        self.assertEqual(note_parts(first, 'footnote')[:2], ('Aprzed[1]po', ['[ref]Przypis']))
        runs = Document(first).paragraphs[0].runs
        self.assertEqual([run.text for run in runs], ['A', 'przed', '', 'po'])
        second = rebuild_docx(BytesIO(first.getvalue()))
        _, texts, root, _ = note_parts(second, 'footnote')
        self.assertEqual(texts, ['[ref]Przypis'])
        self.assertEqual(root.xpath('./w:footnote/@w:type', namespaces=NS), ['separator', 'continuationSeparator'])

    def test_notes_survive_the_whole_preparation_with_the_cleaner(self):
        """Przebudowa → ujednolicenie → miękkie entery → Odkurzacz: treść przypisu poprawiona,
        odsyłacz przesunięty przed kropkę (note_references), bez pominięć."""
        for kind in NOTE_PARTS:
            with self.subTest(kind):
                result = prepare_docx(noted_document(['Tekst.', 1], kind), rebuild=True, normalize_formatting=True,
                                      remove_soft_whitespace=True, cleaner_rules=list(DEFAULT_EDITORIAL_RULES))
                body, texts, _, _ = note_parts(result, kind)
                self.assertEqual((body, texts), ('Tekst[1].', ['[ref] Treść przypisu…']))


class RebuildConversionTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_real_worker_docx_and_mail_default(self):
        doc = Document()
        doc.add_paragraph('Tekst')
        doc.core_properties.author = 'Secret'
        payload = saved(doc).getvalue()
        out, extension, mime = convert_document(
            SimpleUploadedFile('a.docx', payload), [], include_docx=True, rebuild=True, normalize=False)
        with out:
            self.assertEqual(extension, 'docx')
            self.assertIn('wordprocessingml', mime)
            self.assertEqual(Document(out).core_properties.author, '')
        row = {'uid': 1, 'folder': 'Story', 'files': [('story.docx', payload)]}
        for rebuild in (True, False):
            with package_messages([row], clean=False, convert=False, rebuild=rebuild) as archive, \
                    ZipFile(archive) as z:
                content = z.read(z.namelist()[0])
                if rebuild:
                    self.assertEqual(Document(BytesIO(content)).core_properties.author, '')
                else:
                    self.assertEqual(content, payload)

    def test_original_invalid_docx_is_downloadable_from_mail(self):
        data = b'not a valid docx'
        rows = [{'uid': 1, 'folder': 'Text', 'files': [('text.docx', data)]}]
        with package_messages(rows, clean=False, convert=False, rebuild=False) as out, ZipFile(out) as archive:
            self.assertEqual(archive.read(archive.namelist()[0]), data)

    def test_confirmation_still_builds_new_docx(self):
        doc = Document()
        doc.core_properties.author = 'Private'
        doc.add_paragraph('Zachowaj').runs[0].italic = True
        doc.add_table(rows=1, cols=1).cell(0, 0).text = 'Pomiń tabelę'
        doc.add_paragraph('Lista', 'List Number')
        payload = saved(doc).getvalue()
        with self.assertRaises(RebuildConfirmationRequired) as caught:
            convert_document(BytesIO(payload), [], include_docx=True, rebuild=True)
        self.assertIn('tabele wraz z ich treścią', str(caught.exception))
        self.assertIn('tekst pozycji pozostanie', str(caught.exception))
        out, ext, _ = convert_document(
            BytesIO(payload), [], include_docx=True, rebuild=True, allow_rebuild_omissions=True)
        with out:
            result = Document(out)
            self.assertEqual(ext, 'docx')
            self.assertEqual(result.core_properties.author, '')
            self.assertEqual(len(result.tables), 0)
            self.assertEqual([p.text for p in result.paragraphs], ['Zachowaj', 'Lista'])
            self.assertTrue(result.paragraphs[0].runs[0].italic)

    def test_mail_batch_collects_warnings_for_all_files(self):
        rows = []
        for uid, kind in enumerate(('table', 'list'), 1):
            doc = Document()
            doc.add_paragraph('Tekst')
            if kind == 'table':
                doc.add_table(rows=1, cols=1)
            else:
                doc.add_paragraph('Pozycja', 'List Number')
            rows.append({'uid': uid, 'folder': kind, 'files': [(kind + '.docx', saved(doc).getvalue())]})
        with self.assertRaises(RebuildConfirmationRequired) as caught:
            package_messages(rows, clean=False, convert=False)
        self.assertIn('table.docx', str(caught.exception))
        self.assertIn('list.docx', str(caught.exception))
        with package_messages(rows, clean=False, convert=False, allow_rebuild_omissions=True) as out, \
                ZipFile(out) as archive:
            self.assertEqual(len(archive.namelist()), 2)


class RebuildPageTests(IsolatedProgramDirsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser('rebuild')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_rebuild_checkbox_is_off_by_default_and_combines_with_rules(self):
        page = self.client.get(reverse('core:programs'))
        self.assertFalse(page.context['form'].fields['rebuild'].initial)
        self.assertNotContains(page, 'value="rebuild"')
        for enabled in (False, True):
            doc = Document()
            doc.add_paragraph('Zdanie. następne zdanie...')
            doc.core_properties.author = 'Private'
            data = {
                'program_action': 'clean',
                'rules': ['sentence_case'],
                'document': SimpleUploadedFile('a.docx', saved(doc).getvalue()),
            }
            if enabled:
                data['rebuild'] = 'on'
            response = run_program(self.client, data)
            self.assertEqual(response.status_code, 200)
            result = downloaded_document(response)
            self.assertEqual(result.paragraphs[0].text, 'Zdanie. Następne zdanie...')
            self.assertEqual(result.core_properties.author, '' if enabled else 'Private')

    @tag('slow')
    def test_button_download_and_unsupported_error(self):
        for table in (False, True):
            doc = Document()
            doc.add_paragraph('Tekst')
            doc.core_properties.author = 'Private'
            if table:
                doc.add_table(rows=1, cols=1)
            response = run_program(self.client, {
                'program_action': 'clean',
                'rebuild': 'on',
                'document': SimpleUploadedFile('story.docx', saved(doc).getvalue()),
            })
            self.assertEqual(response.status_code, 200)
            if not table:
                self.assertIn('_nowy.docx', response['Content-Disposition'])
                result = downloaded_document(response)
                self.assertEqual(result.core_properties.author, '')
                self.assertEqual(result.paragraphs[0].text, 'Tekst')
                continue
            # Zadanie czeka na potwierdzenie; oryginał zostaje na serwerze tylko do tej chwili.
            self.assertContains(response, 'W nowym DOCX zostaną pominięte')
            self.assertContains(response, 'value="confirm"')
            token = response.context['token']
            with patch.object(jobs, 'launch', side_effect=jobs.execute):
                accepted = self.client.post(reverse('core:program_job', args=[token]),
                                            {'action': 'confirm', 'view': 'page'})
            self.assertEqual(accepted.status_code, 302)
            page = self.client.get(accepted['Location'])
            self.assertEqual(page.context['state']['state'], 'done')
            rebuilt = downloaded_document(self.client.get(page.context['download_url']))
            self.assertEqual(len(rebuilt.tables), 0)
            self.assertEqual(rebuilt.core_properties.author, '')
            self.assertFalse(list(Path(self.work_dir).glob('convert/program_jobs/*/source.docx')))


FIELD_WARNING = 'mechanizm pól automatycznych (zwykły tekst wyniku może pozostać) (liczba: 1)'
TOC_WARNING = 'spis treści (wstaw go ponownie w Wordzie: Odwołania → Spis treści) (liczba: 1)'


def placeholder_run(text, style='PlaceholderText'):
    """Tekst zastępczy kontrolki tak, jak zapisuje go Word (styl znakowy „Tekst zastępczy”)."""
    return f'<w:r><w:rPr><w:rStyle w:val="{style}"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def texts(document, **options):
    return [p.text for p in Document(rebuild_docx(saved(document), **options)).paragraphs]


class RebuildContentControlTests(SimpleTestCase):
    """Kontrolki zawartości znikają, ale ich tekst i formatowanie zostają; tekst zastępczy i spis treści nie."""

    def test_inline_control_keeps_text_and_formatting_without_warning(self):
        document = Document()
        paragraph = document.add_paragraph('Imię: ')
        paragraph._p.append(content_control('<w:r><w:rPr><w:b/><w:i/></w:rPr><w:t>Anna</w:t></w:r>'))
        paragraph.add_run(' i dalej.')
        self.assertEqual(omissions_of(document), [])
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual(rebuilt.paragraphs[0].text, 'Imię: Anna i dalej.')
        name = rebuilt.paragraphs[0].runs[1]
        self.assertEqual(name.text, 'Anna')
        self.assertTrue(name.bold)
        self.assertTrue(name.italic)
        self.assertEqual(rebuilt.element.body.xpath('.//w:sdt'), [])

    def test_block_control_keeps_paragraphs_in_place(self):
        document = Document()
        document.add_paragraph('Przed')
        add_block(document, content_control(
            '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>' + text_run('Pierwszy') + '</w:p>'
            '<w:p>' + text_run('Drugi') + '</w:p>'))
        document.add_paragraph('Po')
        self.assertEqual(omissions_of(document), [])
        rebuilt = Document(rebuild_docx(saved(document)))
        self.assertEqual([p.text for p in rebuilt.paragraphs], ['Przed', 'Pierwszy', 'Drugi', 'Po'])
        self.assertEqual(rebuilt.paragraphs[1].alignment, WD_ALIGN_PARAGRAPH.CENTER)

    def test_nested_controls_are_all_unwrapped(self):
        document = Document()
        outer = add_block(document, content_control('<w:p>' + text_run('Zewnątrz, ') + '</w:p>'))
        outer.find(qn('w:sdtContent')).find(qn('w:p')).append(content_control(text_run('środek')))
        self.assertEqual(omissions_of(document), [])
        self.assertEqual(texts(document), ['Zewnątrz, środek'])

    def test_placeholder_text_is_not_carried(self):
        document = Document()
        paragraph = document.add_paragraph('Data: ')
        paragraph._p.append(content_control(placeholder_run('Kliknij tutaj, aby wprowadzić datę.'), placeholder=True))
        add_block(document, content_control(
            '<w:p>' + placeholder_run('Kliknij lub naciśnij tutaj, aby wprowadzić tekst.') + '</w:p>',
            placeholder=True))
        document.add_paragraph('Koniec')
        self.assertEqual(omissions_of(document), [])
        self.assertEqual(texts(document), ['Data: ', 'Koniec'])

    def test_polish_placeholder_style_is_recognized_by_its_name(self):
        document = Document()
        style = document.styles.add_style('Tekstzastpczy', WD_STYLE_TYPE.CHARACTER)
        style.element.find(qn('w:name')).set(qn('w:val'), 'Placeholder Text')
        style_id = style.element.get(qn('w:styleId'))
        paragraph = document.add_paragraph('Data: ')
        paragraph._p.append(content_control(placeholder_run('Kliknij tutaj.', style_id), placeholder=True))
        self.assertEqual(omissions_of(document), [])
        self.assertEqual(texts(document), ['Data: '])

    def test_placeholder_flag_with_real_content_keeps_the_text_with_a_warning(self):
        """Generator dokumentów wpisał treść, a oznaczenie w:showingPlcHdr zostało – treść nie może zniknąć."""
        document = Document()
        paragraph = document.add_paragraph('Imię: ')
        paragraph._p.append(content_control(text_run('Jan'), placeholder=True))
        add_block(document, content_control('<w:p>' + text_run('TEKST WYPEŁNIONY') + '</w:p>', placeholder=True))
        self.assertEqual(omissions_of(document), [PLACEHOLDER_OMISSION + ' (liczba: 2)'])
        self.assertEqual(texts(document, allow_omissions=True), ['Imię: Jan', 'TEKST WYPEŁNIONY'])

    def test_placeholder_group_with_filled_fields_is_unwrapped(self):
        document = Document()
        from lxml import etree
        inner = etree.tostring(content_control(text_run('KOWALSKI')), encoding=str)
        add_block(document, content_control('<w:p>' + text_run('Nazwisko: ') + inner + '</w:p>', placeholder=True))
        self.assertEqual(omissions_of(document), [PLACEHOLDER_OMISSION + ' (liczba: 1)'])
        self.assertEqual(texts(document, allow_omissions=True), ['Nazwisko: KOWALSKI'])

    def test_table_of_contents_is_removed_with_one_warning(self):
        document = Document()
        entry = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                 '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" </w:instrText></w:r>'
                 '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                 '<w:hyperlink w:anchor="_Toc1"><w:r><w:t>Rozdział 1</w:t></w:r></w:hyperlink>'
                 '<w:fldSimple w:instr="PAGEREF _Toc1"><w:r><w:t>5</w:t></w:r></w:fldSimple>'
                 '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
        add_block(document, content_control(
            '<w:p>' + text_run('Spis treści') + '</w:p>' + entry, gallery='Table of Contents'))
        document.add_heading('Rozdział 1', 1)
        # Pola i odnośniki spisu nie są liczone osobno: znikają razem z nim.
        self.assertEqual(omissions_of(document), [TOC_WARNING])
        self.assertEqual(texts(document, allow_omissions=True), ['Rozdział 1'])

    def test_other_gallery_blocks_are_unwrapped(self):
        document = Document()
        add_block(document, content_control('<w:p>' + text_run('Tytuł książki') + '</w:p>', gallery='Cover Pages'))
        self.assertEqual(omissions_of(document), [])
        self.assertEqual(texts(document), ['Tytuł książki'])

    def test_controls_in_table_rows_and_cells_leave_only_the_table_warning(self):
        document = Document()
        document.add_paragraph('Tekst')
        table = document.add_table(rows=1, cols=1)
        row = table.rows[0]._tr
        cell = row.tc_lst[0]
        cell.append(content_control('<w:p>' + text_run('w komórce') + '</w:p>'))
        table._tbl.append(content_control(
            '<w:tr><w:tc><w:p>' + text_run('wiersz') + '</w:p></w:tc></w:tr>'))
        row.append(content_control('<w:tc><w:p>' + text_run('komórka') + '</w:p></w:tc>'))
        self.assertEqual(omissions_of(document), ['tabele wraz z ich treścią (liczba: 1)'])
        self.assertEqual(texts(document, allow_omissions=True), ['Tekst'])

    def test_section_break_inside_block_control_is_reported(self):
        document = Document()
        add_block(document, content_control(
            '<w:p><w:pPr><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:pPr>'
            + text_run('Część pierwsza') + '</w:p>'))
        document.add_paragraph('Część druga')
        self.assertEqual(omissions_of(document),
                         ['podziały na sekcje i ustawienia ich układu (pozostanie jedna sekcja A4) (liczba: 1)'])


class RebuildSimpleFieldTests(SimpleTestCase):
    """Pole proste (w:fldSimple) zachowuje się jak złożone: wynik zostaje zwykłym tekstem."""

    def test_simple_field_result_stays_as_text(self):
        document = Document()
        paragraph = document.add_paragraph('Autor: ')
        paragraph._p.append(parse_xml(
            '<w:fldSimple %s w:instr=" AUTHOR "><w:r><w:rPr><w:i/></w:rPr><w:t>Jan</w:t></w:r>'
            '<w:fldData>AAAA</w:fldData></w:fldSimple>' % nsdecls('w')))
        self.assertEqual(omissions_of(document), [FIELD_WARNING])
        rebuilt = Document(rebuild_docx(saved(document), allow_omissions=True))
        self.assertEqual(rebuilt.paragraphs[0].text, 'Autor: Jan')
        self.assertTrue(rebuilt.paragraphs[0].runs[1].italic)
        self.assertEqual(rebuilt.element.body.xpath('.//w:fldSimple | .//w:fldData'), [])

    def test_nested_simple_fields_count_once_and_fields_in_tables_are_not_counted(self):
        document = Document()
        document.add_paragraph()._p.append(parse_xml(
            '<w:fldSimple %s w:instr="IF"><w:fldSimple w:instr="PAGE"><w:r><w:t>3</w:t></w:r></w:fldSimple>'
            '</w:fldSimple>' % nsdecls('w')))
        self.assertEqual(omissions_of(document), [FIELD_WARNING])
        self.assertEqual(texts(document, allow_omissions=True), ['3'])
        tabled = Document()
        tabled.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]._p.append(parse_xml(
            '<w:fldSimple %s w:instr="PAGE"><w:r><w:t>1</w:t></w:r></w:fldSimple>' % nsdecls('w')))
        self.assertEqual(omissions_of(tabled), ['tabele wraz z ich treścią (liczba: 1)'])


def book_document():
    """Książka A5 z lustrzanymi marginesami, odległością nagłówka i dwiema kolumnami o różnych szerokościach."""
    document = Document()
    document.add_paragraph('Treść')
    section = document.sections[0]
    section.page_width, section.page_height = Cm(14.8), Cm(21)
    section.top_margin, section.bottom_margin = Cm(2), Cm(2.2)
    section.left_margin, section.right_margin = Cm(1.8), Cm(1.5)
    section.header_distance, section.footer_distance = Cm(0.9), Cm(1.1)
    section.gutter = Cm(0.5)
    columns = section._sectPr.find(qn('w:cols'))
    columns.set(qn('w:num'), '2')
    columns.set(qn('w:equalWidth'), '0')
    columns.set(qn('w:sep'), '1')
    columns.append(parse_xml('<w:col %s w:w="3000" w:space="400"/>' % nsdecls('w')))
    columns.append(parse_xml('<w:col %s w:w="2500"/>' % nsdecls('w')))
    document.settings.element.insert(1, parse_xml('<w:mirrorMargins %s/>' % nsdecls('w')))
    return document


class RebuildKeepPageFormatTests(SimpleTestCase):
    """keep_page_format=True: wynik ma format strony, marginesy i kolumny pierwszej sekcji źródła."""

    def test_book_layout_is_kept_without_warning(self):
        document = book_document()
        inspect_docx(saved(document), keep_page_format=True)
        self.assertEqual(omissions_of(document),
                         ['format strony, orientacja lub kolumny (wynik: A4 pionowo, jedna kolumna) (liczba: 1)'])
        rebuilt = Document(rebuild_docx(saved(document), keep_page_format=True))
        section = rebuilt.sections[0]
        for name, expected in (('page_width', 14.8), ('page_height', 21), ('top_margin', 2),
                               ('bottom_margin', 2.2), ('left_margin', 1.8), ('right_margin', 1.5),
                               ('header_distance', 0.9), ('footer_distance', 1.1), ('gutter', 0.5)):
            self.assertAlmostEqual(getattr(section, name).cm, expected, places=2, msg=name)
        columns = section._sectPr.find(qn('w:cols'))
        self.assertEqual((columns.get(qn('w:num')), columns.get(qn('w:equalWidth')), columns.get(qn('w:sep'))),
                         ('2', '0', '1'))
        self.assertEqual([dict(c.attrib) for c in columns.findall(qn('w:col'))],
                         [{qn('w:w'): '3000', qn('w:space'): '400'}, {qn('w:w'): '2500'}])
        self.assertEqual(len(rebuilt.settings.element.xpath('w:mirrorMargins')), 1)
        self.assertEqual(rebuilt.paragraphs[0].text, 'Treść')

    def test_default_rebuild_still_uses_a4(self):
        rebuilt = Document(rebuild_docx(saved(book_document()), allow_omissions=True))
        section = rebuilt.sections[0]
        self.assertAlmostEqual(section.page_width.cm, 21, places=1)
        self.assertAlmostEqual(section.left_margin.cm, 2.5, places=2)
        self.assertIsNone(section._sectPr.find(qn('w:cols')).get(qn('w:num')))
        self.assertEqual(rebuilt.settings.element.xpath('w:mirrorMargins'), [])

    def test_landscape_orientation_is_kept(self):
        document = Document()
        document.add_paragraph('Treść')
        section = document.sections[0]
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Cm(29.7), Cm(21)
        inspect_docx(saved(document), keep_page_format=True)
        rebuilt = Document(rebuild_docx(saved(document), keep_page_format=True)).sections[0]
        self.assertEqual(rebuilt.orientation, WD_ORIENT.LANDSCAPE)
        self.assertAlmostEqual(rebuilt.page_width.cm, 29.7, places=1)

    def test_missing_source_values_fall_back_to_a4(self):
        document = Document()
        document.add_paragraph('Treść')
        sect_pr = document.sections[0]._sectPr
        for tag_name in ('w:pgSz', 'w:pgMar', 'w:cols'):
            sect_pr.remove(sect_pr.find(qn(tag_name)))
        rebuilt = Document(rebuild_docx(saved(document), keep_page_format=True)).sections[0]
        self.assertAlmostEqual(rebuilt.page_width.cm, 21, places=1)
        self.assertAlmostEqual(rebuilt.top_margin.cm, 2.5, places=2)

    def test_mirror_margins_switched_off_are_not_copied(self):
        document = Document()
        document.add_paragraph('Treść')
        document.settings.element.insert(1, parse_xml('<w:mirrorMargins %s w:val="0"/>' % nsdecls('w')))
        rebuilt = Document(rebuild_docx(saved(document), keep_page_format=True))
        self.assertEqual(rebuilt.settings.element.xpath('w:mirrorMargins'), [])

    def test_several_sections_keep_the_first_layout(self):
        document = book_document()
        document.add_section()
        document.sections[1].page_width = Cm(21)
        with self.assertRaises(RebuildUnsupported) as caught:
            inspect_docx(saved(document), keep_page_format=True)
        self.assertEqual(caught.exception.omissions, [
            'podziały na sekcje i ustawienia ich układu (pozostanie jedna sekcja o układzie pierwszej sekcji) '
            '(liczba: 1)'])
        rebuilt = Document(rebuild_docx(saved(document), allow_omissions=True, keep_page_format=True))
        self.assertEqual(len(rebuilt.sections), 1)
        self.assertAlmostEqual(rebuilt.sections[0].page_width.cm, 14.8, places=1)
