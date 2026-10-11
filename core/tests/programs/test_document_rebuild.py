"""Przebudowa dokumentu do nowego DOCX: zachowana treść i formatowanie, usunięte metadane, ostrzeżenia o pominięciach."""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, RGBColor

from core.program_test_support import run_program
from core.services import program_jobs as jobs
from core.services.document_converter import RebuildConfirmationRequired, convert_document
from core.services.document_rebuild import RebuildUnsupported, inspect_docx, rebuild_docx
from core.services.mailbox_import import package_messages
from fantazmaty.testing import create_superuser

from .support import IsolatedProgramDirsMixin, downloaded_document, saved


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
            'blok bookmarkStart wraz z treścią (liczba: 1)',
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

    def test_one_equation_is_counted_once(self):
        """Regresja: części równania (m:r, m:t) nie są liczone jako osobne równania."""
        document = Document()
        document.add_paragraph('Wzór')._p.append(
            parse_xml('<m:oMath %s><m:r><m:t>x</m:t></m:r></m:oMath>' % nsdecls('m')))
        with self.assertRaisesMessage(RebuildUnsupported, 'równania matematyczne (liczba: 1)'):
            inspect_docx(saved(document))


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
