"""Przebudowa dokumentu do nowego DOCX: zachowana treść i formatowanie, usunięte metadane, ostrzeżenia o pominięciach."""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm

from core.program_test_support import run_program
from core.services import program_jobs as jobs
from core.services.document_converter import RebuildConfirmationRequired, convert_document
from core.services.document_rebuild import RebuildUnsupported, inspect_docx, rebuild_docx
from core.services.mailbox_import import package_messages

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

    @tag('slow')
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
        cls.user = get_user_model().objects.create_superuser('rebuild', 'rebuild@example.com', 'testpassword')

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
