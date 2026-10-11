"""Odkurzacz: czyszczenie DOCX we wszystkich częściach dokumentu oraz strona programu z pobraniem wyniku."""
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor
from lxml import html

from core.program_test_support import run_program
from core.services.document_preparation import prepare_docx
from core.services.odkurzacz import (
    DEFAULT_EDITORIAL_RULES, EDITORIAL_RULES, FORM_DEFAULT_EDITORIAL_RULES, clean_docx,
)
from fantazmaty.testing import create_member

from .support import (
    IsolatedProgramDirsMixin, docx_bytes, downloaded, downloaded_document, footnote_text, formatted_docx, saved,
    upload, whole_document,
)


def soft_whitespace_document():
    """Twarde spacje, miękki enter i podział strony w akapicie, tabeli i nagłówku."""
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run('Ala\u00a0ma')
    run.bold = True
    run.add_break()
    run.add_text('kota\u202f!')
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    doc.add_paragraph('Drugi akapit')
    doc.add_table(rows=1, cols=1).cell(0, 0).text = 'Tabela\u00a0A\nB'
    doc.sections[0].header.paragraphs[0].text = 'Nagłówek\u00a0A\nB'
    return saved(doc).getvalue()


class CleanDocxTests(SimpleTestCase):
    def test_correction_across_runs_also_covers_tables_and_headers(self):
        document = Document()
        para = document.add_paragraph()
        para.add_run('Ala ').bold = True
        para.add_run(' ma kota...').underline = True
        document.add_table(rows=1, cols=1).cell(0, 0).text = 'Bez  zmian...'
        document.sections[0].header.paragraphs[0].text = 'Nagłówek  bez zmian...'
        result = Document(clean_docx(saved(document), ['spaces', 'ellipsis']))
        self.assertEqual(result.paragraphs[0].text, 'Ala ma kota…')
        self.assertTrue(result.paragraphs[0].runs[0].bold)
        self.assertTrue(result.paragraphs[0].runs[1].underline)
        self.assertEqual(result.tables[0].cell(0, 0).text, 'Bez zmian…')
        self.assertEqual(result.sections[0].header.paragraphs[0].text, 'Nagłówek bez zmian…')
        self.assertFalse(result.paragraphs[0]._p.xpath('.//w:color | .//w:highlight'))

    def test_default_rules_cover_tables_headers_and_footnotes(self):
        result = clean_docx(whole_document(), sorted(DEFAULT_EDITORIAL_RULES))
        document = Document(BytesIO(result.getvalue()))
        self.assertEqual(document.paragraphs[0].text, 'Akapit główny.')
        self.assertEqual(document.tables[0].cell(0, 0).text, 'Lampa świeciła, lampa gasła.')
        self.assertEqual(document.sections[0].header.paragraphs[0].text, 'Nagłówek strony')
        self.assertIn('Przypis z\u00a0lampą i\u00a0lampą.', footnote_text(BytesIO(result.getvalue())))

    def test_all_rules_run(self):
        result = Document(clean_docx(BytesIO(docx_bytes('  Ala  ma kota...  ')), [key for key, _ in EDITORIAL_RULES]))
        self.assertEqual(result.paragraphs[0].text, 'Ala ma kota…')

    def test_oversize_text_is_rejected(self):
        with self.assertRaises(ValueError):
            clean_docx(BytesIO(docx_bytes('a' * 20001)), ['spaces'])

    def test_tabs_are_replaced_with_single_spaces(self):
        text = '\tAla\tma \t kota\t\t'
        result = Document(clean_docx(BytesIO(docx_bytes(text)), ['tabs']))
        self.assertEqual(result.paragraphs[0].text, ' Ala ma kota ')
        self.assertFalse(result.paragraphs[0]._p.xpath('.//w:tab'))
        unchanged = Document(clean_docx(BytesIO(docx_bytes(text)), []))
        self.assertEqual(unchanged.paragraphs[0].text, text)

    def test_dialogue_spaces_survive_internal_docx_markers(self):
        """Regresja: znaczniki proofErr/bookmark między przebiegami gubiły spację przed półpauzą."""
        cases = (
            ('viraptorka', ' – pochwalił.'),
            ('rozumiemy.', ' – W jej głos'),
            ('pszczół.', ' – Roześmiała się'),
            ('viraptorka – ', 'pochwalił.'),
        )
        rules = ['hyphen_dash', 'dash_spaces', 'trim', 'before_punct', 'after_punct']
        for marker in ('proofErr', 'bookmarkStart', 'bookmarkEnd'):
            for left, right in cases:
                with self.subTest(marker=marker, left=left):
                    document = Document()
                    paragraph = document.add_paragraph()
                    paragraph.add_run(left).bold = True
                    paragraph._p.append(OxmlElement('w:' + marker))
                    paragraph.add_run(right).italic = True
                    result = Document(clean_docx(saved(document), rules))
                    self.assertEqual(result.paragraphs[0].text, left + right)
                    self.assertTrue(result.paragraphs[0].runs[0].bold)
                    self.assertTrue(result.paragraphs[0].runs[-1].italic)

    def test_soft_whitespace_keeps_paragraphs_page_breaks_formatting_and_cleans_tables(self):
        with prepare_docx(BytesIO(soft_whitespace_document()), use_cleaner=True, cleaner_rules=['single_nbsp'],
                          remove_soft_whitespace=True) as stream:
            doc = Document(stream)
        self.assertEqual([p.text for p in doc.paragraphs[:2]], ['Ala ma', 'kota !'])
        self.assertEqual(len(doc.paragraphs), 3)
        self.assertTrue(doc.paragraphs[0].runs[0].bold)
        self.assertEqual(len(doc.element.xpath('//w:br[@w:type="page"]')), 1)
        self.assertEqual(len(doc.element.xpath('//w:br[not(@w:type) or @w:type="textWrapping"]')), 0)
        self.assertEqual([p.text for p in doc.tables[0].cell(0, 0).paragraphs], ['Tabela A', 'B'])
        self.assertEqual([p.text for p in doc.sections[0].header.paragraphs], ['Nagłówek A', 'B'])


class CleanDocxStructureTests(SimpleTestCase):
    """Odkurzacz nie zmienia pól Worda ani pustych akapitów, które niosą układ dokumentu."""

    def test_field_code_and_result_are_left_alone(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run('Przed  polem ')
        for kind, text in (('begin', None), (None, ' HYPERLINK  "x" '), ('separate', None), (None, 'Wynik  pola'),
                           ('end', None)):
            run = paragraph.add_run()
            if kind:
                marker = OxmlElement('w:fldChar')
                marker.set(qn('w:fldCharType'), kind)
                run._r.append(marker)
            else:
                code = OxmlElement('w:instrText' if 'HYPERLINK' in text else 'w:t')
                code.text = text
                code.set(qn('xml:space'), 'preserve')
                run._r.append(code)
        paragraph.add_run(' i  po nim')
        result = Document(clean_docx(saved(document), ['spaces']))
        self.assertEqual(result.paragraphs[0].text, 'Przed polem Wynik  pola i po nim')
        self.assertEqual(result.paragraphs[0]._p.xpath('.//w:instrText/text()'), [' HYPERLINK  "x" '])

    def test_empty_paragraphs_with_layout_are_kept(self):
        document = Document()
        document.add_paragraph('A')
        document.add_paragraph(' \u00a0').add_run().add_tab()
        document.add_paragraph()                                            # usuwany: drugi pusty z rzędu
        document.add_paragraph().paragraph_format.page_break_before = True  # podział strony
        document.add_paragraph('', style='List Number')                      # numeracja
        document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)          # treść inna niż tekst
        bookmarked = document.add_paragraph()
        bookmarked._p.append(OxmlElement('w:bookmarkStart'))
        document.add_paragraph()
        document.add_table(rows=1, cols=1)
        document.add_paragraph()
        document.add_paragraph('B')
        result = Document(clean_docx(saved(document), ['empty_paragraphs']))
        self.assertEqual(len(result.paragraphs), len(document.paragraphs) - 1)
        self.assertEqual(result.paragraphs[2].paragraph_format.page_break_before, True)

    def test_unknown_rule_is_refused(self):
        with self.assertRaisesMessage(ValueError, 'Nieznana reguła'):
            clean_docx(BytesIO(docx_bytes()), ['nieznana'])


class OdkurzaczPageTests(IsolatedProgramDirsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('member', 'Redaktor')
        cls.outsider = get_user_model().objects.create_user('outsider', 'outsider@example.com')

    def setUp(self):
        super().setUp()
        self.url = reverse('core:programs')
        self.client.force_login(self.member)

    def test_page_has_all_rules_and_submit_above_them(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Odkurzacz')
        self.assertContains(response, 'name="rules"', count=len(EDITORIAL_RULES))
        checked = sum(' checked' in str(checkbox.tag()) for checkbox in response.context['form']['rules'])
        self.assertEqual(checked, len(FORM_DEFAULT_EDITORIAL_RULES))
        self.assertNotContains(response, 'gifrific')
        page = response.content.decode()
        self.assertLess(page.index('Odkurz i pobierz DOCX'), page.index('<legend>Opcje korekty'))
        self.assertIn('odkurzacz-upload', page)

    def test_access_and_csrf(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'document': upload()}).status_code, 302)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'document': upload()}).status_code, 403)
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.member)
        self.assertEqual(secure_client.post(self.url, {'document': upload()}).status_code, 403)

    def test_invalid_uploads_and_rules(self):
        for data in ({}, {'document': upload(b'bad')}, {'document': upload(name='tekst.txt')},
                     {'document': upload(), 'rules': ['unknown']}):
            with self.subTest(keys=list(data)):
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
                self.assertFalse(response.streaming)

    def test_download_uses_selected_rules(self):
        response = run_program(self.client, {'document': upload(), 'rules': ['spaces']})
        self.assertEqual(response.status_code, 200)
        self.assertIn('tekst_odkurzony.docx', response['Content-Disposition'])
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(downloaded_document(response).paragraphs[0].text, 'Ala ma kota...')

    def test_no_rules_preserves_text_and_formatting(self):
        document = Document()
        run = document.add_paragraph().add_run('Ala  ma kota...')
        run.bold = True
        run.underline = True
        run.font.color.rgb = RGBColor(12, 34, 56)
        response = run_program(self.client, {'document': upload(saved(document).getvalue())})
        run = downloaded_document(response).paragraphs[0].runs[0]
        self.assertEqual(run.text, 'Ala  ma kota...')
        self.assertTrue(run.bold)
        self.assertTrue(run.underline)
        self.assertEqual(run.font.color.rgb, RGBColor(12, 34, 56))

    def test_without_formatting_option_font_and_alignment_stay(self):
        response = run_program(self.client, {'document': upload(formatted_docx()), 'rules': ['spaces']})
        doc = downloaded_document(response)
        self.assertEqual(doc.paragraphs[0].runs[0].font.size.pt, 17)
        self.assertEqual(doc.paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.LEFT)

    def test_formatting_option_applies_mailbox_typography_with_and_without_rebuild(self):
        for rebuild in (False, True):
            with self.subTest(rebuild=rebuild):
                data = {'document': upload(formatted_docx()), 'normalize_formatting': 'on', 'rules': ['spaces']}
                if rebuild:
                    data['rebuild'] = 'on'
                response = run_program(self.client, data)
                self.assertEqual(response.status_code, 200)
                first, center = downloaded_document(response).paragraphs
                self.assertEqual(first.text, 'Ala ma kota.')
                self.assertEqual(first.runs[0].font.name, 'Times New Roman')
                self.assertEqual(first.runs[0].font.size.pt, 12)
                self.assertTrue(first.runs[0].italic)
                self.assertEqual(first.alignment, WD_ALIGN_PARAGRAPH.JUSTIFY)
                self.assertEqual(first.paragraph_format.line_spacing, 1.5)
                self.assertEqual(center.alignment, WD_ALIGN_PARAGRAPH.CENTER)
                self.assertEqual(center.paragraph_format.first_line_indent, 0)

    def test_failure_is_reported_on_the_job_page(self):
        with patch('core.services.program_jobs.convert_document', side_effect=ValueError('bad package')):
            response = run_program(self.client, {'document': upload(), 'rules': ['spaces']})
        self.assertContains(response, 'Nie udało się przetworzyć dokumentu')
        self.assertContains(response, 'Wróć do programów')
        self.assertNotIn('bad package', response.content.decode())

    @tag('slow')
    def test_soft_whitespace_checkbox_reaches_cleaner_and_conversion_worker(self):
        response = run_program(self.client, {
            'document': SimpleUploadedFile('tekst.docx', soft_whitespace_document()),
            'remove_soft_whitespace': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('\u00a0', downloaded_document(response).paragraphs[0].text)
        response = run_program(self.client, {
            'program_action': 'convert',
            'convert-document': SimpleUploadedFile('tekst.docx', soft_whitespace_document()),
            'convert-formats': ['epub'],
            'convert-use_cleaner': 'on',
        })
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(downloaded(response))) as archive:
            content = archive.read('EPUB/content.xhtml')
        self.assertEqual(
            [p.text_content().strip() for p in html.fromstring(content).xpath('//p')][:2],
            ['Ala ma', 'kota!'],  # Odkurzacz usuwa też spację przed wykrzyknikiem.
        )
