"""Dialogi zapisane jako lista punktowana: wykrywanie (tylko kwestie z punktorem-kreską w kilku miejscach tekstu),
zamiana na akapity z myślnikiem i formatowaniem zwykłego akapitu, dołączanie urwanych wierszy, śledzone zmiany,
kolejność kroków przygotowania, opcja programów (domyślnie włączona), import ze skrzynki i pełna konwersja."""
import json
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase, TestCase, tag
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm

from core.odkurzacz_forms import DocumentConversionForm, MergeForm, OdkurzaczForm
from core.services.document_conversion_worker import dialog_lists_note, main
from core.services.document_dialog_lists import dialog_lists_docx
from core.services.document_preparation import prepare_docx
from core.services.odkurzacz import DEFAULT_EDITORIAL_RULES
from core.views.programs import job_options

from .support import saved
from .test_pdf_rendering import pdf_html_spy
from .test_conversion_worker import WorkerDirectoryMixin

# Punktory: kreska z Autoformatowania Worda, łącznik w foncie Symbol, kropka i numeracja liczbami.
LEVELS = {90: ('bullet', '-', ''), 91: ('bullet', '', '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr>'),
          92: ('bullet', '•', ''), 93: ('decimal', '%1.', '')}
DASH, SYMBOL_DASH, DOT, NUMBERED = LEVELS

DIALOG = [('body', 'Zapadła cisza.'), (DASH, 'Dziwka - odezwała się kupa szmat.'), (DASH, 'Seksworkerka - odparła.'),
          ('body', 'Roxie spojrzała na niego.'), (DASH, 'Co mi powiesz?')]


def numbered_document(paragraphs, *, list_style='List Paragraph', style_numbering=None):
    """DOCX z akapitami (rodzaj, tekst): 'body' – akapit z wcięciem 1,25 cm, 'cont' – akapit bez wcięcia (urwany
    wiersz), 'heading', 'tab' – akapit zaczynający się tabulatorem, liczba – punkt listy z tym numId."""
    document = Document()
    numbering = document.part.numbering_part.element
    first_num = numbering.find(qn('w:num'))
    for num_id, (fmt, text, rpr) in LEVELS.items():
        first_num.addprevious(parse_xml(
            f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{num_id}"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
            f'<w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/><w:pPr><w:ind w:left="720" w:hanging="360"/>'
            f'</w:pPr>{rpr}</w:lvl></w:abstractNum>'))
        numbering.append(parse_xml(f'<w:num {nsdecls("w")} w:numId="{num_id}"><w:abstractNumId w:val="{num_id}"/></w:num>'))
    if style_numbering:
        properties = document.styles['List Bullet'].element.get_or_add_pPr()
        for node in properties.findall(qn('w:numPr')):
            properties.remove(node)
        properties.append(parse_xml(f'<w:numPr {nsdecls("w")}><w:numId w:val="{style_numbering}"/></w:numPr>'))
    for kind, text in paragraphs:
        if kind == 'heading':
            document.add_heading(text, 1)
            continue
        paragraph = document.add_paragraph(text.lstrip('\t') if kind == 'tab' else '')
        if kind == 'tab':
            paragraph.runs[0].text = '\t' + text.lstrip('\t')
        elif kind == 'style':
            paragraph.style = document.styles['List Bullet']
            paragraph.add_run(text)
        elif isinstance(kind, int):
            paragraph.style = document.styles[list_style]
            paragraph.add_run(text)
            paragraph._p.get_or_add_pPr().append(parse_xml(
                f'<w:numPr {nsdecls("w")}><w:ilvl w:val="0"/><w:numId w:val="{kind}"/></w:numPr>'))
            paragraph.paragraph_format.left_indent = Cm(1.27)
        else:
            paragraph.add_run(text)
        if kind == 'body':
            paragraph.paragraph_format.first_line_indent = Cm(1.25)
        elif kind in ('cont', 'tab'):
            paragraph.paragraph_format.first_line_indent = 0
            paragraph.paragraph_format.left_indent = 0
    return saved(document).getvalue()


def converted(paragraphs, **options):
    report = {}
    result = dialog_lists_docx(BytesIO(numbered_document(paragraphs, **options)), report)
    return Document(result), report


def listed(paragraph):
    properties = paragraph._p.pPr
    node = properties.find(f'{qn("w:numPr")}/{qn("w:numId")}') if properties is not None else None
    return node is not None and node.get(qn('w:val')) != '0'


class DetectionTests(SimpleTestCase):
    def test_only_dialogs_with_dash_bullets_are_changed(self):
        """Kwestie z punktorem-kreską (też łącznik w foncie Symbol) w kilku miejscach tekstu albo obok kwestii
        zapisanych prawidłowo są zamieniane – wszystkie, także bez zdań; zwykłe wyliczenie, punktor-kropka,
        numeracja, jedno miejsce bez wtrąceń i pojedyncza kwestia bez prawidłowych kwestii zostają listą."""
        shopping = [('body', 'Kupiła:'), (DASH, 'jabłka'), (DASH, 'gruszki'), ('body', 'I wróciła.'), (DASH, 'mleko')]
        for name, paragraphs, expected in (
            ('kwestie z kreską', DIALOG, 3),
            ('kreska w foncie Symbol', [(SYMBOL_DASH if isinstance(kind, int) else kind, text) for kind, text in DIALOG], 3),
            ('trzy miejsca, kwestie bez zdań', [(DASH, 'Tak'), ('body', 'Cisza.'), (DASH, 'Nie'), ('body', 'Cisza.'),
                                                (DASH, 'Może')], 3),
            ('dwa miejsca, wtrącenie, reszta bez zdań', [(DASH, 'Hej - zawołał'), ('body', 'Cisza.'), (DASH, 'Co'),
                                                          (DASH, 'Nic')], 3),
            ('obok prawidłowych kwestii', [('body', '– Tak – rzekł.'), ('body', 'Cisza.'), (DASH, 'Nie'),
                                           ('body', '-Może?')], 1),
            ('wyliczenie bez zdań', shopping, 0),
            ('punktor-kropka', [(DOT if isinstance(kind, int) else kind, text) for kind, text in DIALOG], 0),
            ('numeracja', [(NUMBERED if isinstance(kind, int) else kind, text) for kind, text in DIALOG], 0),
            ('jedno miejsce bez wtrąceń', [('body', 'Wstęp.'), (DASH, 'Pierwsza rada.'), (DASH, 'Druga rada.')], 0),
            ('jedna kwestia', [('body', 'Wstęp.'), (DASH, 'Tak - rzekł.')], 0),
        ):
            with self.subTest(name):
                source = numbered_document(paragraphs)
                report = {}
                result = dialog_lists_docx(BytesIO(source), report)
                self.assertEqual(report, {'dialogs': expected, 'merged': 0})
                document = Document(result)
                dialogs = [p.text for p, (kind, _) in zip(document.paragraphs, paragraphs)
                           if isinstance(kind, int) and p.text.startswith('– ')]
                self.assertEqual(len(dialogs), expected)
                self.assertEqual(sum(listed(p) for p in document.paragraphs),
                                 0 if expected else sum(isinstance(kind, int) for kind, _ in paragraphs))
                if not expected:
                    self.assertEqual(result.getvalue(), source)  # plik bez zmian, bajt w bajt

    def test_document_without_numbering_or_lists_is_unchanged(self):
        document = Document()
        document.add_paragraph('Tekst bez list.')
        source = saved(document).getvalue()
        report = {}
        self.assertEqual(dialog_lists_docx(BytesIO(source), report).getvalue(), source)
        self.assertEqual(report, {'dialogs': 0, 'merged': 0})


    def test_files_from_other_editors_with_lists_are_unchanged(self):
        """Pliki LibreOffice, Google Docs, Worda (365, 2007, Strict), WPS, OnlyOffice i Pages z listami i zmianami
        śledzonymi zostają bez zmian (nie są dialogami)."""
        from . import editor_fixtures as editors, editor_fixtures_exotic as exotic
        cases = {'LibreOffice': editors.libreoffice_docx(footnotes=True, text_box=True),
                 'Google Docs': editors.google_docs_docx(footnotes=True)}
        cases.update((name, getattr(exotic, name)()) for name in (
            'word365_docx', 'word2007_docx', 'strict_docx', 'wps_docx', 'onlyoffice_docx', 'pages_docx',
            'google_docs_extras_docx', 'minimal_docx', 'review_docx', 'libreoffice_review_docx'))
        for name, data in cases.items():
            with self.subTest(name):
                data = data.getvalue() if hasattr(data, 'getvalue') else data
                report = {}
                self.assertEqual(dialog_lists_docx(BytesIO(data), report).getvalue(), data)
                self.assertEqual(report, {'dialogs': 0, 'merged': 0})


class ConversionTests(SimpleTestCase):
    def test_dialog_gets_dash_and_body_paragraph_layout(self):
        """Kwestia: „– ” na początku (bez kursywy pierwszego przebiegu), bez numeracji, styl i wcięcie
        najczęstszego zwykłego akapitu z wcięciem pierwszego wiersza; kwestia zaczynająca się kreską bez drugiej."""
        source = Document(BytesIO(numbered_document(DIALOG + [(DASH, '- Już mam kreskę.')])))
        source.paragraphs[2].runs[0].italic = True
        source.paragraphs[2].runs[0].text = '  ' + source.paragraphs[2].runs[0].text
        document = Document(dialog_lists_docx(saved(source)))
        texts = [p.text for p in document.paragraphs]
        self.assertEqual(texts, ['Zapadła cisza.', '– Dziwka - odezwała się kupa szmat.', '– Seksworkerka - odparła.',
                                 'Roxie spojrzała na niego.', '– Co mi powiesz?', '- Już mam kreskę.'])
        for paragraph in document.paragraphs[1:]:
            with self.subTest(paragraph.text):
                self.assertFalse(listed(paragraph))
                self.assertEqual(paragraph.style.name, 'Normal')
                self.assertEqual((paragraph.paragraph_format.first_line_indent, paragraph.paragraph_format.left_indent),
                                 (document.paragraphs[0].paragraph_format.first_line_indent, None))
        self.assertGreater(document.paragraphs[0].paragraph_format.first_line_indent, Cm(1.2))
        dash, italic = document.paragraphs[2].runs[:2]
        self.assertEqual((dash.text, dash.italic, italic.text, italic.italic), ('– ', None, 'Seksworkerka - odparła.', True))

    def test_layout_comes_from_nearest_plain_paragraph(self):
        """Bez akapitów z wcięciem pierwszego wiersza kwestia bierze formatowanie najbliższego wcześniejszego
        akapitu bez lewego marginesu (margines bywa pozostałością listy, dołączony wiersz nie jest wzorem)."""
        source = Document(BytesIO(numbered_document([
            ('cont', 'Na początku.'), ('cont', 'Po liście.'), ('cont', 'Przed kwestią.'), (DASH, 'Tak - rzekł'),
            ('cont', 'cicho.'), ('cont', 'Środek.'), (DASH, 'Nie - odparła.')])))
        first, leftover, before = source.paragraphs[:3]
        leftover.paragraph_format.left_indent = Cm(1.27)
        before.paragraph_format.space_before = Cm(0.4)
        report = {}
        document = Document(dialog_lists_docx(saved(source), report))
        self.assertEqual(report, {'dialogs': 2, 'merged': 1})
        dialog, middle, last = document.paragraphs[3:]
        self.assertEqual((dialog.text, last.text), ('– Tak - rzekł cicho.', '– Nie - odparła.'))
        self.assertEqual((dialog.paragraph_format.space_before, dialog.paragraph_format.left_indent),
                         (before.paragraph_format.space_before, 0))
        self.assertEqual((last.paragraph_format.space_before, last.paragraph_format.left_indent), (None, 0))

    def test_numbering_from_list_style_is_switched_off(self):
        """Punkty z numeracją ze stylu („Lista punktowana”) dostają styl zwykłego akapitu, a gdyby został styl
        z numeracją – numId 0."""
        document, report = converted([('body', 'Cisza.'), ('style', 'Tak - rzekł.'), ('body', 'Cisza.'),
                                      ('style', 'Nie - odparła.')], style_numbering=DASH)
        self.assertEqual(report['dialogs'], 2)
        self.assertEqual([(p.text, p.style.name, listed(p)) for p in document.paragraphs[1::2]],
                         [('– Tak - rzekł.', 'Normal', False), ('– Nie - odparła.', 'Normal', False)])

    def test_broken_dialog_lines_are_joined(self):
        """Urwana kwestia (bez znaku końca zdania) dostaje następne akapity bez wcięcia; akapit z wcięciem,
        tabulatorem, nagłówek, punkt listy albo kwestia zakończona zdaniem zostają osobno."""
        document, report = converted([
            ('body', 'Szedł.'),
            (DASH, 'Nie podchodź tam! - matka szarpnęła go - święto Dołu'), ('cont', 'będzie jutro,'),
            ('cont', 'wtedy pójdziemy razem!'), ('cont', 'Ostasz spojrzał na nią.'),
            (DASH, 'A byłbyś uprzejmy - Ostasz dawno już nie grzeszył'), ('tab', 'cierpliwością.'),
            (DASH, 'Daj mi spokój! - krzyknął'), ('body', 'starzec.'),
            (DASH, 'Kto - zapytał'), ('heading', 'Rozdział drugi'),
            (DASH, 'Co - szepnął'), (DASH, 'Nic.'),
        ])
        self.assertEqual(report, {'dialogs': 6, 'merged': 2})
        self.assertEqual([p.text for p in document.paragraphs], [
            'Szedł.', '– Nie podchodź tam! - matka szarpnęła go - święto Dołu będzie jutro, wtedy pójdziemy razem!',
            'Ostasz spojrzał na nią.', '– A byłbyś uprzejmy - Ostasz dawno już nie grzeszył', '\tcierpliwością.',
            '– Daj mi spokój! - krzyknął', 'starzec.', '– Kto - zapytał', 'Rozdział drugi', '– Co - szepnął', '– Nic.'])

    def test_tracked_changes_are_respected(self):
        """Tekst usunięty śledzoną zmianą nie liczy się do wykrywania, a akapit ze zmianą znaku akapitu nie jest
        łączony z następnym."""
        source = Document(BytesIO(numbered_document(DIALOG[:3] + [('cont', 'dalszy ciąg')])))
        deleted = parse_xml(f'<w:del {nsdecls("w")} w:id="7" w:author="R" w:date="2026-11-05T00:00:00Z"><w:r>'
                            '<w:delText>Usunięte.</w:delText></w:r></w:del>')
        source.paragraphs[2]._p.append(deleted)
        source.paragraphs[2].runs[0].text = 'Seksworkerka - odparła'
        source.paragraphs[2]._p.get_or_add_pPr().append(parse_xml(
            f'<w:rPr {nsdecls("w")}><w:ins w:id="8" w:author="R" w:date="2026-11-05T00:00:00Z"/></w:rPr>'))
        source.paragraphs[0].insert_paragraph_before('Wstęp.')
        body = Document(BytesIO(numbered_document([('body', 'Przerwa.'), (DASH, 'I jeszcze - rzekł.')])))
        for element in body.element.body.iterchildren(qn('w:p')):
            source.element.body.append(element)
        report = {}
        document = Document(dialog_lists_docx(saved(source), report))
        self.assertEqual(report, {'dialogs': 3, 'merged': 0})
        self.assertIn('dalszy ciąg', [p.text for p in document.paragraphs])
        self.assertIn(b'Usuni\xc4\x99te.', ZipFile(saved(document)).read('word/document.xml'))


class PreparationTests(SimpleTestCase):
    def test_runs_before_rebuild_and_cleaner_only_when_enabled(self):
        """Krok działa przed przebudową i Odkurzaczem (łącznik we wtrąceniu staje się półpauzą), tylko z opcją;
        raport dostaje liczby zamian."""
        source = numbered_document(DIALOG + [(DASH, 'Tak - rzekł')] + [('cont', 'cicho.')])
        for name, options, first in (
            ('bez opcji', {}, 'Dziwka - odezwała się kupa szmat.'),
            ('sama opcja', {'dialog_lists': True}, '– Dziwka - odezwała się kupa szmat.'),
            ('z Odkurzaczem i przebudową', {'dialog_lists': True, 'rebuild': True, 'allow_omissions': True,
                                             'use_cleaner': True, 'cleaner_rules': list(DEFAULT_EDITORIAL_RULES)},
             '– Dziwka – odezwała się kupa szmat.'),
        ):
            with self.subTest(name):
                report = {}
                document = Document(prepare_docx(BytesIO(source), dialog_report=report, **options))
                self.assertEqual(document.paragraphs[1].text, first)
                self.assertEqual(any(listed(p) for p in document.paragraphs), not options)
                self.assertEqual(report, {'dialogs': 4, 'merged': 1} if options else {})
                if options:
                    self.assertTrue(document.paragraphs[-1].text.endswith('rzekł cicho.'))

    def test_note_lists_counts(self):
        self.assertEqual(dialog_lists_note({'dialogs': 3, 'merged': 0}),
                         'Dialogi zapisane jako lista punktowana zamieniono na akapity z myślnikiem dialogowym '
                         '(kwestie: 3).')
        self.assertTrue(dialog_lists_note({'dialogs': 3, 'merged': 2}).endswith('(kwestie: 3, dołączone urwane wiersze: 2).'))


@tag('docx')
class WorkerTests(WorkerDirectoryMixin, SimpleTestCase):
    def run_worker(self, **config):
        (self.directory / 'source.docx').write_bytes(numbered_document(DIALOG))
        (self.directory / 'job.json').write_text(json.dumps({'title': 'Dialogi', 'prepare': True, **config}))
        with patch('sys.argv', ['worker', str(self.directory)]):
            return main()

    def warnings(self):
        path = self.directory / 'warnings.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def test_docx_job_and_epub_keep_the_note(self):
        """Zadanie bez formatów (Odkurzacz) i konwersja EPUB: uwaga o zamianie, EPUB bez punktów listy; bez opcji
        kwestie zostają punktami."""
        note = dialog_lists_note({'dialogs': 3, 'merged': 0})
        for config, expected in (({'formats': [], 'include_docx': True, 'dialog_lists': True, 'normalize': False}, [note]),
                                 ({'formats': ['epub'], 'dialog_lists': True}, [note]),
                                 ({'formats': ['epub']}, [])):
            with self.subTest(config=config):
                (self.directory / 'warnings.json').unlink(missing_ok=True)
                self.assertEqual(self.run_worker(**config), 0)
                self.assertEqual(self.warnings(), expected)
                if config['formats']:
                    with ZipFile(self.directory / 'document.epub') as epub:
                        content = epub.read('EPUB/content.xhtml').decode()
                    self.assertEqual('<ul' in content, not expected)
                    self.assertEqual('– Dziwka' in content, bool(expected))

    @tag('slow')
    def test_pdf_shows_dialog_dashes_instead_of_bullets(self):
        """PDF: kwestie jako akapity z myślnikiem; bez opcji – punkty listy."""
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                captured = []
                with pdf_html_spy(captured):
                    self.assertEqual(self.run_worker(formats=['pdf'], dialog_lists=enabled), 0)
                self.assertTrue((self.directory / 'document.pdf').read_bytes().startswith(b'%PDF'))
                markup = ''.join(part for part, _ in captured)
                self.assertEqual('– Dziwka' in markup, enabled)
                self.assertEqual('• Dziwka' in markup, not enabled)


class ProgramOptionTests(TestCase):
    def test_option_is_on_by_default_and_reaches_the_job(self):
        """Odkurzacz, scalanie i konwerter: opcja domyślnie włączona i przekazywana do zadania."""
        for form_class, action in ((OdkurzaczForm, 'clean'), (MergeForm, 'merge'), (DocumentConversionForm, 'convert')):
            with self.subTest(action):
                form = form_class()
                self.assertIs(form.fields['dialog_lists'].initial, True)
                for value in (True, False):
                    form.cleaned_data = {name: field.initial for name, field in form.fields.items()}
                    form.cleaned_data.update(dialog_lists=value, rules=[], formats=['epub'])
                    self.assertIs(job_options(action, form)['dialog_lists'], value)

    def test_programs_page_shows_checked_option_in_three_programs(self):
        from fantazmaty.testing import create_superuser
        from lxml import html
        from django.urls import reverse
        self.client.force_login(create_superuser('dialogi-admin'))
        page = html.fromstring(self.client.get(reverse('core:programs')).content.decode())
        boxes = page.xpath('//input[@type="checkbox"][substring(@name, string-length(@name) - 11) = "dialog_lists"]')
        # Odkurzacz bez prefiksu pól, konwerter i scalanie z prefiksami.
        self.assertEqual(sorted(box.get('name') for box in boxes), ['convert-dialog_lists', 'dialog_lists',
                                                                    'merge-dialog_lists'])
        self.assertTrue(all(box.get('checked') is not None for box in boxes))
