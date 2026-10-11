"""Odkurzacz: czyszczenie DOCX we wszystkich częściach dokumentu oraz strona programu z pobraniem wyniku."""
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor
from lxml import html

from core.program_test_support import run_program
from core.services.document_preparation import prepare_docx
from core.services.odkurzacz import (
    ALL_EDITORIAL_RULES, DEFAULT_EDITORIAL_RULES, EDITORIAL_RULES, FORM_DEFAULT_EDITORIAL_RULES, clean_docx,
)
from fantazmaty.testing import create_member

from .support import (
    IsolatedProgramDirsMixin, docx_bytes, downloaded, downloaded_document, footnote_text, formatted_docx, saved,
    set_default_language, set_language, tracked_changes_document, upload, whole_document,
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


class CleanDocxMarkerAndOrderTests(SimpleTestCase):
    """Znaczniki Worda bez treści nie dzielą zdania, a Odkurzacz działa po usunięciu miękkich spacji."""

    def test_zero_width_word_markers_do_not_split_the_sentence(self):
        """Regresja: proofErr/zakładka/lastRenderedPageBreak przerywały korektę („domu  .Potem”)."""
        for marker, inside_run in (('proofErr', False), ('bookmarkStart', False), ('commentRangeStart', False),
                                   ('lastRenderedPageBreak', True)):
            with self.subTest(marker=marker):
                document = Document()
                paragraph = document.add_paragraph()
                paragraph.add_run('Wrócił do domu ').bold = True
                element = OxmlElement('w:' + marker)
                if inside_run:
                    run = paragraph.add_run()
                    run._r.append(element)
                    run.add_text(' .potem')
                else:
                    paragraph._p.append(element)
                    paragraph.add_run(' .potem')
                result = Document(clean_docx(saved(document), FORM_DEFAULT_EDITORIAL_RULES))
                self.assertEqual(result.paragraphs[0].text, 'Wrócił do domu. Potem')
                self.assertTrue(result.paragraphs[0].runs[0].bold)
                self.assertEqual(len(result.paragraphs[0]._p.xpath('.//w:' + marker)), 1)

    def test_nonbreaking_spaces_added_by_the_cleaner_survive_soft_whitespace_removal(self):
        """Regresja: zamiana twardych spacji szła po Odkurzaczu i kasowała spacje dodane przez jego reguły."""
        document = Document()
        run = document.add_paragraph().add_run('Był w\u00a0domu i ogrodzie,')
        run.add_break()
        run.add_text('miał\u00a012345 zł.')
        with prepare_docx(saved(document), use_cleaner=True, cleaner_rules=list(FORM_DEFAULT_EDITORIAL_RULES),
                          remove_soft_whitespace=True) as stream:
            result = Document(stream)
        self.assertEqual([p.text for p in result.paragraphs],
                         ['Był w\u00a0domu i\u00a0ogrodzie,', 'miał 12\u00a0345\u00a0zł.'])


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


W_DEL, W_INS = qn('w:del'), qn('w:ins')


def _resolve_revisions(root, accept):
    """Programowe „Zaakceptuj wszystkie” / „Odrzuć wszystkie” zmiany w drzewie XML jednej części."""
    remove, unwrap = (W_DEL, W_INS) if accept else (W_INS, W_DEL)
    for paragraph in list(root.iter(qn('w:p'))):
        mark = paragraph.find(f"{qn('w:pPr')}/{qn('w:rPr')}/{W_DEL}")
        if mark is None:
            continue
        mark.getparent().remove(mark)
        following = paragraph.getnext()
        if accept and following is not None and following.tag == qn('w:p'):
            # Usunięty znak akapitu: treść łączy się z następnym akapitem.
            position = 1 if following.find(qn('w:pPr')) is not None else 0
            for child in [child for child in paragraph if child.tag != qn('w:pPr')]:
                following.insert(position, child)
                position += 1
            paragraph.getparent().remove(paragraph)
    for element in list(root.iter(remove)):
        element.getparent().remove(element)
    for element in list(root.iter(unwrap)):
        for child in list(element):
            element.addprevious(child)
        element.getparent().remove(element)
    for node in root.iter(qn('w:delText')):
        node.tag = qn('w:t')


def _story_texts(stream, resolve=None):
    """Teksty akapitów wszystkich części (treść, tabele, pola tekstowe, nagłówki, stopki, przypisy)."""
    from core.services.document_stories import document_stories
    from docx.text.paragraph import Paragraph
    stream.seek(0)
    stories, _ = document_stories(Document(stream))
    texts = []
    for story in stories:
        if resolve is not None:
            _resolve_revisions(story.root, accept=resolve == 'accept')
        texts.append([Paragraph(element, None).text for element in story.root.iter(qn('w:p'))])
    return texts


def _revisions(stream):
    stream.seek(0)
    with ZipFile(stream) as archive:
        parts = {name: archive.read(name) for name in archive.namelist() if name.endswith('.xml')}
    from lxml import etree
    found = []
    for name, data in parts.items():
        root = etree.fromstring(data)
        found.extend((name, element) for element in root.iter(W_DEL, W_INS))
    return found


class TrackChangesTests(SimpleTestCase):
    """track_changes=True: poprawki jako śledzone zmiany Worda we wszystkich częściach dokumentu."""

    rules = sorted(ALL_EDITORIAL_RULES)

    def test_accept_all_gives_the_untracked_result_and_reject_all_the_original(self):
        source = tracked_changes_document().getvalue()
        untracked = clean_docx(BytesIO(source), self.rules)
        tracked = clean_docx(BytesIO(source), self.rules, track_changes=True)
        Document(BytesIO(tracked.getvalue()))  # wynik otwiera się ponownie
        expected = _story_texts(untracked, 'accept')
        self.assertEqual(_story_texts(BytesIO(tracked.getvalue()), 'accept'), expected)
        self.assertEqual(_story_texts(BytesIO(tracked.getvalue()), 'reject'),
                         _story_texts(BytesIO(source), 'reject'))
        # Zmieniły się wszystkie części: treść, tabela, pole tekstowe, nagłówek, stopka, przypis.
        changed_parts = {name for name, element in _revisions(BytesIO(tracked.getvalue()))
                         if element.get(qn('w:author')) == 'Odkurzacz'}
        self.assertTrue({'word/document.xml', 'word/footnotes.xml'} <= changed_parts)
        self.assertTrue(any('header' in name for name in changed_parts))
        self.assertTrue(any('footer' in name for name in changed_parts))
        self.assertIn('Tabela z\u00a0kropkami…', expected[0])
        self.assertIn('Pole tekstowe…', expected[0])

    def test_revision_ids_are_unique_and_attributes_complete(self):
        tracked = clean_docx(tracked_changes_document(), self.rules, track_changes=True)
        revisions = _revisions(tracked)
        ours = [element for _, element in revisions if element.get(qn('w:author')) == 'Odkurzacz']
        self.assertGreater(len(ours), 10)
        ids = [element.get(qn('w:id')) for _, element in revisions]
        self.assertEqual(len(ids), len(set(ids)))
        # Także względem innych identyfikatorów (zakładki, przypisy, istniejąca wstawka w:id=9).
        self.assertTrue(all(int(element.get(qn('w:id'))) > 9 for element in ours))
        for element in ours:
            self.assertRegex(element.get(qn('w:date')), r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertTrue(any(element.get(qn('w:author')) == 'Redakcja' for _, element in revisions))
        with ZipFile(tracked) as archive:
            self.assertNotIn('word/comments.xml', archive.namelist())

    def test_changes_keep_run_formatting_and_are_word_granular(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run('Ala  ma ').bold = True
        paragraph.add_run('kota... Potem pickupami.').italic = True
        result = Document(clean_docx(saved(document), ['spaces', 'ellipsis', 'user_word_corrections'],
                                     track_changes=True))
        p = result.paragraphs[0]._p
        deleted = [(node.text, node.getparent().find(qn('w:rPr'))) for node in p.iter(qn('w:delText'))]
        inserted = [(node.text, node.getparent().find(qn('w:rPr'))) for node in p.xpath('./w:ins//w:t')]
        self.assertEqual([text for text, _ in deleted], [' ', '...', 'pickupami'])
        self.assertEqual([text for text, _ in inserted], ['…', 'pikapami'])
        self.assertIsNotNone(deleted[0][1].find(qn('w:b')))
        self.assertTrue(all(props.find(qn('w:i')) is not None for _, props in deleted[1:] + inserted))
        # Niezmieniony tekst zostaje w zwykłych przebiegach z formatowaniem źródła.
        self.assertEqual(result.paragraphs[0].text, 'Ala ma kota Potem .')
        self.assertTrue(result.paragraphs[0].runs[0].bold)

    def test_single_character_change_marks_only_that_word(self):
        result = Document(clean_docx(BytesIO(docx_bytes('Długi akapit, w którym Ona coś robi.')), ['pronouns_lower'],
                                     track_changes=True))
        p = result.paragraphs[0]._p
        self.assertEqual(p.xpath('.//w:delText/text()'), ['Ona'])
        self.assertEqual(p.xpath('./w:ins//w:t/text()'), ['ona'])

    def test_tabs_and_text_in_one_run_are_tracked(self):
        result = clean_docx(BytesIO(docx_bytes('\tAla\t \tma')), ['tabs', 'trim'], track_changes=True)
        self.assertEqual(_story_texts(result, 'accept')[0], ['Ala ma'])
        self.assertEqual(_story_texts(result, 'reject')[0], ['\tAla\t \tma'])
        self.assertTrue(Document(BytesIO(result.getvalue())).paragraphs[0]._p.xpath('./w:del/w:r/w:tab'))

    def test_empty_paragraph_merge_is_a_tracked_paragraph_deletion(self):
        document = Document()
        for text in ('A', '', '  ', '', 'B', '', 'C'):
            document.add_paragraph(text)
        source = saved(document).getvalue()
        rules = ['empty_paragraphs', 'trim']
        untracked = clean_docx(BytesIO(source), rules)
        tracked = clean_docx(BytesIO(source), rules, track_changes=True)
        self.assertEqual(_story_texts(tracked, 'accept'), _story_texts(untracked))
        self.assertEqual(_story_texts(tracked, 'accept')[0], ['A', '', 'B', '', 'C'])
        self.assertEqual(_story_texts(tracked, 'reject')[0], ['A', '', '  ', '', 'B', '', 'C'])
        paragraphs = Document(BytesIO(tracked.getvalue())).paragraphs
        self.assertEqual(len(paragraphs), 7)
        self.assertEqual([bool(p._p.xpath('./w:pPr/w:rPr/w:del')) for p in paragraphs],
                         [False, True, True, False, False, False, False])

    def test_without_changes_the_document_has_no_revisions(self):
        tracked = clean_docx(BytesIO(docx_bytes('Tekst bez zmian.')), self.rules, track_changes=True)
        self.assertEqual(_revisions(tracked), [])


class CleanerSummaryTests(SimpleTestCase):
    def test_summary_counts_changes_per_rule_without_changing_the_result(self):
        source = tracked_changes_document().getvalue()
        rules = sorted(ALL_EDITORIAL_RULES)
        summary = {'stare': 1}
        counted = clean_docx(BytesIO(source), rules, summary=summary)
        plain = clean_docx(BytesIO(source), rules)
        self.assertEqual(_story_texts(counted), _story_texts(plain))
        # Słownik jest uzupełniany (inne kroki mogą dopisać swoje klucze).
        self.assertEqual(summary.pop('stare'), 1)
        self.assertEqual(summary['empty_paragraphs'], 3)
        self.assertEqual(summary['user_word_corrections'], 1)
        self.assertEqual(summary['dates_times'], 1)
        self.assertEqual(summary['ellipsis'], 6)  # akapit, tabela, pole tekstowe (2 kopie), stopka, przypis
        self.assertTrue(all(value > 0 for value in summary.values()))
        self.assertLessEqual(set(summary), set(rules))

    def test_counts_for_simple_text(self):
        summary = {}
        clean_docx(BytesIO(docx_bytes('Ala  ma  kota... i psa.')), ['spaces', 'ellipsis', 'single_nbsp', 'quotes'],
                   summary=summary)
        self.assertEqual(summary, {'spaces': 2, 'ellipsis': 1, 'single_nbsp': 1})
        summary = {}
        clean_docx(BytesIO(docx_bytes('Bez zmian.')), ['spaces'], summary=summary)
        self.assertEqual(summary, {})

    def test_summary_also_counts_tracked_changes(self):
        summary = {}
        clean_docx(BytesIO(docx_bytes('Dnia 1.2.2025 o 9:5 wrócił.')), ['dates_times'], track_changes=True,
                   summary=summary)
        self.assertEqual(summary, {'dates_times': 1})


class LanguageTests(SimpleTestCase):
    """Fragmenty oznaczone w Wordzie innym językiem niż polski nie są poprawiane."""

    rules = ['spaces', 'ellipsis', 'user_word_corrections', 'pronouns_lower']

    def cleaned(self, document, rules=None, **kwargs):
        return Document(clean_docx(saved(document), rules or self.rules, **kwargs)).paragraphs

    def test_foreign_run_is_a_barrier_and_polish_text_around_is_cleaned(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run('Mówił  wtedy... ')
        set_language(paragraph.add_run('Pickup  truck...'), 'en-US')
        paragraph.add_run(' i  pickupem...')
        result = self.cleaned(document)
        self.assertEqual(result[0].text, 'Mówił wtedy… Pickup  truck... i pikapem…')

    def test_polish_and_empty_marks_are_polish(self):
        for value in ('pl-PL', 'pl', 'PL-pl', '', 'x-none'):
            with self.subTest(value=value):
                document = Document()
                set_language(document.add_paragraph().add_run('Ala  ma...'), value)
                self.assertEqual(self.cleaned(document)[0].text, 'Ala ma…')

    def test_character_and_paragraph_styles_mark_the_language(self):
        document = Document()
        set_default_language(document, 'pl-PL')  # jak w polskim Wordzie
        set_language(document.styles['Emphasis'].element, 'de-DE')
        set_language(document.styles['Quote'].element, 'en-GB')
        paragraph = document.add_paragraph()
        paragraph.add_run('Nein  danke... ', style='Emphasis')
        paragraph.add_run('Tak  jest...')
        quote = document.add_paragraph(style='Quote')
        quote.add_run('To  be...')
        set_language(quote.add_run(' a  to po polsku...'), 'pl-PL')
        result = self.cleaned(document)
        self.assertEqual(result[0].text, 'Nein  danke... Tak jest…')
        self.assertEqual(result[1].text, 'To  be... a to po polsku…')

    def test_paragraph_style_inherits_language_from_its_base_style(self):
        document = Document()
        set_language(document.styles['Quote'].element, 'fr-FR')
        style = document.styles.add_style('Cytat francuski', WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = document.styles['Quote']
        document.add_paragraph('Titre  du...', style='Cytat francuski')
        self.assertEqual(self.cleaned(document)[0].text, 'Titre  du...')

    def test_document_default_language(self):
        # python-docx (jak angielski Word i Google Docs) ma w docDefaults en-US: bez jawnych
        # oznaczeń polskich traktujemy to jako pozostałość szablonu i tekst jest poprawiany.
        document = Document()
        document.add_paragraph('Ala  ma...')
        self.assertEqual(self.cleaned(document)[0].text, 'Ala ma…')
        # Gdy dokument oznacza polski tekst jawnie, nieoznaczony tekst ma język domyślny.
        set_language(document.add_paragraph().add_run('Polski  tekst...'), 'pl-PL')
        result = self.cleaned(document)
        self.assertEqual([p.text for p in result], ['Ala  ma...', 'Polski tekst…'])
        # Polskie docDefaults: wszystko po polsku, oprócz jawnie obcych fragmentów.
        document = Document()
        set_default_language(document, 'pl-PL')
        document.add_paragraph('Ala  ma...')
        set_language(document.add_paragraph().add_run('Hello  world...'), 'en-US')
        self.assertEqual([p.text for p in self.cleaned(document)], ['Ala ma…', 'Hello  world...'])

    def test_foreign_text_in_headers_and_tracked_mode(self):
        document = Document()
        header = document.sections[0].header.paragraphs[0]
        set_language(header.add_run('Running  head...'), 'en-US')
        header.add_run(' Strona  główna...')
        paragraph = document.add_paragraph()
        set_language(paragraph.add_run('Ich  bin... '), 'de-AT')
        paragraph.add_run('a  ja...')
        stream = clean_docx(saved(document), self.rules, track_changes=True)
        texts = _story_texts(stream, 'accept')
        self.assertIn('Ich  bin... a ja…', texts[0])
        self.assertIn('Running  head... Strona główna…', [text for story in texts[1:] for text in story])
        stream.seek(0)
        deleted = Document(stream).paragraphs[0]._p.xpath('.//w:delText/text()')
        self.assertEqual(deleted, [' ', '...'])


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
