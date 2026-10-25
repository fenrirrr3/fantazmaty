"""Proces roboczy konwertera (uruchamiany w tym procesie): bootstrap i limit pamięci, bezpieczny HTML, akapity list,
rozdziały, odnośniki i obrazy EPUB, podziały stron i separatory scen, nagłówki według reguły nagłówków, kopia DOCX
dla Mammotha, uwagi po polsku, raport błędu bez treści dokumentu oraz praca jako skrypt (bez pakietu core.services)."""
import json
import os
import re
import runpy
import struct
import sys
import zlib
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

import mammoth
from django.test import SimpleTestCase, tag
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from fpdf import FPDF
from lxml import etree, html
from PIL import Image

import core.services
from core.services import document_progress
from core.services.document_conversion_worker import (
    PDF_FAILED_NOTE, convert, cross_chapter_links, main, polish_warnings, sanitize_html, split_chapters,
)
from core.services.document_errors import DocumentInputError
from core.services.document_html import (
    MAMMOTH_NOTES, docx_paragraph_key, list_paragraphs, mammoth_source, paragraph_queues, scene_break,
    split_page_breaks,
)
from core.services.document_stories import document_stories

from .editor_fixtures import R_NS, WORD_PART
from .editor_fixtures_exotic import (
    STYLES, chart_and_smart_art, frames_and_drop_cap, localized_style_names, note_reference, notes_part, p, r,
    word_docx,
)
from .support import (
    OUTLINE_CASES, add_footnote_reference, add_leading_break, add_page_break, add_text_box, add_tracked_insertion,
    outline_document, png, with_footnote,
)


XHTML = '{http://www.w3.org/1999/xhtml}'
BOOTSTRAP = Path(core.services.__file__).with_name('document_worker_bootstrap.py')
SERVICES = BOOTSTRAP.parent
SCRIPT_MODULES = {path.stem for path in SERVICES.glob('*.py')}


class WorkerDirectoryMixin:
    """Katalog zadania procesu roboczego w katalogu tymczasowym testu."""

    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # Proces roboczy kieruje raport postępu do katalogu zadania; po teście wyłączamy go.
        progress = patch.object(document_progress, '_target', None)
        progress.start()
        self.addCleanup(progress.stop)


def png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)


@contextmanager
def script_mode(directory):
    """Katalog core/services na sys.path (jak dla skryptu) i argv procesu; po wyjściu sprzątamy kopie modułów."""
    before = set(sys.modules)
    with patch.object(sys, 'path', [str(SERVICES), *sys.path]), \
            patch.object(sys, 'argv', ['document_worker_bootstrap.py', str(directory)]):
        try:
            yield
        finally:
            for name in set(sys.modules) - before:
                if name in SCRIPT_MODULES:
                    del sys.modules[name]


class SanitizedHtmlTests(SimpleTestCase):
    def test_only_allowed_tags_attributes_and_links_survive(self):
        content = sanitize_html(
            'Wstęp<!-- komentarz --><script>alert(1)</script><p onclick="x()" id="a">Tekst <font>bez</font></p>'
            '<a href="javascript:alert(1)">zły</a><a href="https://example.test">dobry</a><a href="#przypis">#</a>'
            '<img src="assets/image-1.png" alt="Opis" onerror="x()"><img src="https://example.test/x.png">'
            '<table><tr><td colspan="2" rowspan="999">A</td></tr></table>'
            '<h1>Tytuł</h1><h2 id="wlasny">Rozdział</h2>',
            {'assets/image-1.png': b'png'})
        tree = html.fragment_fromstring(content, create_parent='div')
        self.assertTrue(content.startswith('Wstęp'))
        self.assertNotIn('komentarz', content)
        self.assertFalse(tree.xpath('//script | //font | //@onclick | //@onerror'))
        self.assertEqual(tree.xpath('//p/@id'), ['a'])
        self.assertEqual(tree.xpath('//a/@href'), ['https://example.test', '#przypis'])
        self.assertEqual(tree.xpath('//img/@src'), ['assets/image-1.png'])
        self.assertEqual(tree.xpath('//img/@alt'), ['Opis'])
        self.assertEqual((tree.xpath('//td/@colspan'), tree.xpath('//td/@rowspan')), (['2'], []))
        self.assertEqual(tree.xpath('//h1/@id | //h2/@id'), ['section-1', 'wlasny'])

    def test_external_links_are_valid_urls(self):
        """Regresja (epubcheck RSC-020): znaki niedozwolone w adresie z Worda psuły walidację EPUB."""
        content = sanitize_html('<a href="http://example.com/a%20b?x=<1>&amp;y=ą z|w">x</a>'
                                '<a href="mailto:jan kowalski@example.com">y</a>', {})
        self.assertEqual(html.fragment_fromstring(content, create_parent='div').xpath('//a/@href'), [
            'http://example.com/a%20b?x=%3C1%3E&y=%C4%85%20z%7Cw', 'mailto:jan%20kowalski@example.com'])

    def test_list_items_keep_their_paragraph_boundaries(self):
        root = html.fragment_fromstring('<ul><li>Pierwszy <em>akapit</em><ol><li>Zagnieżdżony</li></ol>Ogon</li>'
                                        '<li><p>Już akapit</p></li></ul>', create_parent='div')
        list_paragraphs(root)
        outer = root.xpath('./ul/li')
        self.assertEqual([child.tag for child in outer[0]], ['p', 'ol', 'p'])
        self.assertEqual([p.text_content() for p in outer[0].xpath('./p')], ['Pierwszy akapit', 'Ogon'])
        self.assertEqual(root.xpath('.//ol/li/p/text()'), ['Zagnieżdżony'])
        self.assertEqual([child.tag for child in outer[1]], ['p'])


class EpubPresentationTests(SimpleTestCase):
    def test_chapters_follow_the_repeated_heading_level(self):
        content = ('<h1 id="section-1">Tytuł</h1><p>Wstęp</p><h2 id="section-2">Rozdział 1</h2><p>A</p>'
                   '<h3 id="section-3">Część</h3><p>B</p><h2 id="section-4">Rozdział 2</h2><p>C</p>')
        chapters = split_chapters(content, 'Dokument')
        self.assertEqual([title for title, _, _ in chapters], ['Dokument', 'Rozdział 1', 'Rozdział 2'])
        self.assertEqual([anchor for anchor, _ in chapters[1][2]], ['section-2', 'section-3'])
        # Dokument bez nagłówków to jeden rozdział; pusty początek przed pierwszym rozdziałem znika.
        self.assertEqual(len(split_chapters('<p>Jeden</p><p>Dwa</p>', 'Dokument')), 1)
        chapters = split_chapters('<p> </p><h2>A</h2><p>1</p><h2>B</h2><p>2</p>', 'Dokument')
        self.assertEqual([title for title, _, _ in chapters], ['A', 'B'])

    def test_warnings_are_polish_and_grouped(self):
        warnings = polish_warnings([
            'Unrecognised paragraph style: Cytat (Style ID: Cytat)',
            'Unrecognised paragraph style: Cytat (Style ID: Cytat)',
            'Something new',
        ])
        self.assertEqual(warnings[0], 'Styl akapitu „Cytat” nie ma odpowiednika w e-booku – tekst zachowano '
                                      'w zwykłym formacie. (2 razy)')
        self.assertEqual(warnings[1], 'Uwaga konwertera: Something new')

    def test_undefined_style_is_reported_once(self):
        """Mammoth zgłasza brakujący styl dwa razy, drugi raz jako styl „None”."""
        warnings = polish_warnings([
            'Run style with ID CommentReference was referenced but not defined in the document',
            'Unrecognised run style: None (Style ID: CommentReference)',
        ])
        self.assertEqual(warnings, ['Dokument odwołuje się do nieistniejącego stylu (CommentReference) – '
                                    'pominięto jego formatowanie.'])

    def test_only_links_to_other_chapters_get_a_file_name(self):
        chapters = [
            ('chapter-1.xhtml', '<p id="a">A <a href="#a">tutaj</a> <a href="#brak">nigdzie</a></p>'),
            ('chapter-2.xhtml', '<p>B <a href="#a">do pierwszego</a> <a href="#b">tutaj</a></p><p id="b">C</p>'),
        ]
        first, second = cross_chapter_links(chapters)
        self.assertEqual(first, chapters[0][1])  # rozdział bez odnośników do innych plików zostaje bez zmian
        self.assertIn('href="chapter-1.xhtml#a"', second)
        self.assertIn('href="#b"', second)
        self.assertEqual(cross_chapter_links(chapters[:1]), [chapters[0][1]])

    def test_warnings_about_equations_attachments_shapes_and_linked_images(self):
        """Uwagi o równaniach, wstawkach altChunk i obrazach połączonych są po polsku; wygląd kształtów VML
        (wypełnienie, obrys) z zapasowej wersji pola tekstowego – np. z LibreOffice – nie jest zgłaszany."""
        unrecognised = 'An unrecognised element was ignored: '
        cases = {
            unrecognised + '{http://schemas.openxmlformats.org/officeDocument/2006/math}oMathPara':
                'Pominięto równanie (wzór matematyczny) – e-book i PDF go nie zawierają.',
            unrecognised + '{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath':
                'Pominięto równanie (wzór matematyczny) – e-book i PDF go nie zawierają.',
            unrecognised + 'w:altChunk': 'Pominięto treść dołączoną z innego pliku (w:altChunk) – otwórz dokument '
                                          'w Wordzie i zapisz go ponownie, aby ją scalić.',
            "could not open external image 'file:///C:/Obrazy/a.jpg', external file access is disabled":
                'Pominięto obraz połączony z plikiem spoza dokumentu – wstaw obraz do DOCX.',
            unrecognised + 'v:fill': None,
            unrecognised + 'v:stroke': None,
            unrecognised + 'o:lock': None,
            unrecognised + 'w10:wrap': None,
            unrecognised + 'w:ruby': 'Pominięto nieobsługiwany element dokumentu (w:ruby).',
        }
        for message, expected in cases.items():
            with self.subTest(message):
                self.assertEqual(polish_warnings([message]), [expected] if expected else [])


@tag('slow', 'docx')
class EmbeddedImageTests(WorkerDirectoryMixin, SimpleTestCase):
    def source(self, *pictures):
        """DOCX z obrazami; ich bajty w paczce zastępujemy podanymi (kolejność plików word/media)."""
        document = Document()
        document.add_paragraph('Ilustracja')
        for _ in pictures:
            document.add_picture(png(4, 4))
        original = BytesIO()
        document.save(original)
        path = self.directory / 'source.docx'
        replacements = iter(pictures)
        with ZipFile(original) as source, ZipFile(path, 'w') as target:
            for item in source.infolist():
                data = source.read(item)
                if item.filename.startswith('word/media/'):
                    data = next(replacements)
                target.writestr(item, data)
        return path

    def test_images_are_rasterized_into_epub_and_pdf(self):
        convert(self.source(png(20, 10).getvalue()), self.directory, ['epub', 'pdf'], 'Obraz')
        with ZipFile(self.directory / 'document.epub') as epub:
            self.assertIn('EPUB/assets/image-1.png', epub.namelist())
            self.assertIn('src="assets/image-1.png"', epub.read('EPUB/content.xhtml').decode())
        self.assertIn(b'/Subtype /Image', (self.directory / 'document.pdf').read_bytes())

    def test_unusable_images_are_refused_with_a_public_code(self):
        """Do e-booka trafiają tylko obrazy rastrowe (nie np. ICO podpisane jako PNG). Regresja: obraz EMF/WMF albo
        uszkodzony kończył konwersję komunikatem „DOCX: ValueError”; obraz ponad 20 MB lub 20 mln pikseli – za duży."""
        icon = BytesIO()
        Image.new('RGB', (16, 16)).save(icon, 'ICO')
        header = png(4, 4).getvalue()[:33]  # sygnatura i IHDR: Pillow otwiera plik, ale nie wczyta danych
        cases = (
            ('ICO zamiast PNG', icon.getvalue(), 'unsupported_image'),
            ('EMF/WMF', b'\x01\x00\x00\x00EMF?', 'unsupported_image'),
            ('ucięte dane obrazu', header + png_chunk(b'IDAT', zlib.compress(b'\x00' * 3)[:4]), 'unsupported_image'),
            ('ponad 20 MB', png(4, 4).getvalue() + b'\x00' * (20 * 1024 * 1024), 'image_too_large'),
            ('ponad 20 mln pikseli', png(5000, 5000).getvalue(), 'image_too_large'),
        )
        for label, picture, code in cases:
            with self.subTest(label), self.assertRaises(DocumentInputError) as raised:
                convert(self.source(picture), self.directory, ['epub'], 'Obraz')
            self.assertEqual(raised.exception.public_code, code)

    def test_images_over_the_total_limit_are_refused(self):
        """Każdy obraz mieści się w limicie, ale razem po rasteryzacji przekraczają 30 MB."""
        noise = BytesIO()
        Image.frombytes('RGB', (2150, 2150), os.urandom(2150 * 2150 * 3)).save(noise, 'PNG', compress_level=0)
        with self.assertRaises(DocumentInputError) as raised:
            convert(self.source(noise.getvalue(), noise.getvalue()), self.directory, ['epub'], 'Obraz')
        self.assertEqual(raised.exception.public_code, 'too_many_images')


@tag('docx')
class WorkerMainTests(WorkerDirectoryMixin, SimpleTestCase):
    """Wywołanie main() tak jak bootstrap procesu: katalog zadania w argv, wynik w plikach katalogu."""

    def run_worker(self, document, **config):
        document.save(self.directory / 'source.docx')
        (self.directory / 'job.json').write_text(json.dumps({'formats': [], 'title': 'Test', **config}))
        with patch('sys.argv', ['worker', str(self.directory)]):
            return main()

    def report(self):
        return json.loads((self.directory / 'error.json').read_text(encoding='utf-8'))

    @contextmanager
    def simple_lemmas(self):
        with patch('core.services.document_repetitions._lemma_map',
                   side_effect=lambda words: {word.lower(): word.lower() for word in words}):
            yield

    def document(self, text='Koty koty.'):
        document = Document()
        document.add_paragraph(text)
        return document

    def test_job_without_known_output_is_rejected(self):
        self.assertEqual(self.run_worker(self.document()), 3)
        self.assertEqual(self.run_worker(self.document(), formats=['mobi']), 3)
        self.assertFalse((self.directory / 'error.json').exists())

    def test_inspection_reports_rebuild_omissions(self):
        self.assertEqual(self.run_worker(self.document(), inspect=True), 0)
        document = self.document()
        document.add_table(rows=1, cols=1)
        self.assertEqual(self.run_worker(document, inspect=True), 3)
        report = self.report()
        self.assertEqual(report['error'], 'RebuildUnsupported')
        self.assertEqual(report['omissions'], ['tabele wraz z ich treścią (liczba: 1)'])

    def test_split_writes_the_archive_and_report_or_the_missing_heading_error(self):
        """Tryb podziału: ZIP z plikami opowiadań i split.json; bez Nagłówka 1 – sam raport błędu, bez archiwum."""
        story = Document()
        story.add_heading('Jedyne', level=1)
        story.add_paragraph('Treść.')
        for document, report, archive in ((story, {'files': ['01 Jedyne.docx'], 'titles': ['Jedyne'], 'skipped_intro': False}, True),
                                          (self.document(), {'error': 'no_heading'}, False)):
            with self.subTest(report=report):
                self.assertEqual(self.run_worker(document, split=True), 0)
                self.assertEqual(json.loads((self.directory / 'split.json').read_text(encoding='utf-8')), report)
                self.assertEqual((self.directory / 'document.zip').exists(), archive)
                if archive:
                    with ZipFile(self.directory / 'document.zip') as result:
                        self.assertEqual(result.namelist(), report['files'])

    def test_repetitions_replace_the_source_with_the_marked_document(self):
        """Powtórzenia zastępują źródło oznaczonym DOCX; lematy w kontekście liczy prawdziwy model językowy."""
        with self.simple_lemmas():
            code = self.run_worker(self.document(), include_docx=True, repetitions={
                'analysis_options': {'long_sentences': False, 'long_paragraphs': False}})
        self.assertEqual(code, 0)
        marked = Document(self.directory / 'source.docx').paragraphs[0]
        self.assertEqual(marked.text, 'Koty koty.')
        self.assertTrue(any(run.font.color.rgb for run in marked.runs))

    def test_failure_report_names_only_the_public_reason_and_environment(self):
        code = self.run_worker(self.document('PRYWATNY ' * 2500), include_docx=True, repetitions={})
        self.assertEqual(code, 3)
        report = self.report()
        self.assertEqual((report['stage'], report['error'], report['public_code']),
                         ('Powtórzenia', 'DocumentInputError', 'analysis_limit'))
        self.assertIn('python-docx', report['versions'])
        self.assertTrue(all(set(frame) == {'file', 'line', 'function'} for frame in report['frames']))
        self.assertNotIn('PRYWATNY', json.dumps(report))

    def test_missing_library_exits_with_code_two(self):
        with patch('core.services.document_conversion_worker.convert', side_effect=ImportError('tajne')):
            self.assertEqual(self.run_worker(self.document(), formats=['pdf'], prepare=True), 2)
        self.assertEqual(self.report()['error'], 'ImportError')
        self.assertNotIn('tajne', json.dumps(self.report()))

    def test_failure_is_returned_even_when_the_report_cannot_be_saved(self):
        document = Document()
        document.add_paragraph('Tekst')
        document.save(self.directory / 'source.docx')
        (self.directory / 'job.json').write_text(json.dumps({'formats': ['epub'], 'title': 'T'}))
        (self.directory / 'error.json').mkdir()  # zapis raportu kończy się błędem systemu plików
        with patch('sys.argv', ['worker', str(self.directory)]), \
                patch('core.services.document_conversion_worker.convert', side_effect=RuntimeError):
            self.assertEqual(main(), 3)
        self.assertTrue((self.directory / 'error.json').is_dir())


class BootstrapTests(WorkerDirectoryMixin, SimpleTestCase):
    """document_worker_bootstrap.py: limit pamięci procesu i raport błędu startu bez treści wyjątku."""

    def run_bootstrap(self, **patches):
        """Uruchom bootstrap jako __main__ z podmienionym startem procesu roboczego; zwraca kod wyjścia lub None."""
        runner = runpy.run_path
        (self.directory / 'job.json').write_text(json.dumps({}), encoding='utf-8')
        with patch('sys.argv', ['bootstrap', str(self.directory)]), patch('runpy.run_path', **patches):
            try:
                runner(str(BOOTSTRAP), run_name='__main__')
            except SystemExit as stop:
                return stop.code
        return None

    def report(self):
        return json.loads((self.directory / 'error.json').read_text(encoding='utf-8'))

    def test_startup_failures_are_reported_without_private_details(self):
        """Regresja: błąd importu przed startem procesu roboczego ujawniał treść wyjątku (kod 2). Brak pamięci –
        także wtedy, gdy podczas jego obsługi wystąpił kolejny błąd – to raport MemoryError (kod 3)."""
        def nested(*args, **kwargs):
            try:
                raise MemoryError
            except MemoryError:
                raise ValueError('PRYWATNE dane z dokumentu') from None

        cases = (('błąd importu', ImportError('PRYWATNE dane logowania'), 2, 'ImportError'),
                 ('brak pamięci', MemoryError, 3, 'MemoryError'),
                 ('brak pamięci w trakcie innego błędu', nested, 3, 'MemoryError'))
        for label, failure, code, error in cases:
            with self.subTest(label), patch('resource.setrlimit'):
                self.assertEqual(self.run_bootstrap(side_effect=failure), code)
                report = self.report()
                self.assertEqual((report['stage'], report['error']), ('konwersja', error))
                self.assertTrue(report['frames'])
                self.assertNotIn('PRYWATNE', json.dumps(report))

    def test_bootstrap_limits_the_worker_memory(self):
        """Mały DOCX z ogromnym XML zjadał gigabajty pamięci serwera: proces dostaje limit przestrzeni adresowej
        (domyślny także przy uszkodzonym job.json)."""
        import resource
        runner = runpy.run_path
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        for config, expected in (({}, 1536), ({'memory_limit_mb': 2048}, 2048), ({'memory_limit_mb': True}, 1536),
                                 ({'memory_limit_mb': 0}, None), ('{uszkodzony', 1536)):
            (self.directory / 'job.json').write_text(config if isinstance(config, str) else json.dumps(config))
            with self.subTest(config=config), patch('sys.argv', ['bootstrap', str(self.directory)]), \
                    patch('runpy.run_path'), patch('resource.setrlimit') as setrlimit:
                runner(str(BOOTSTRAP), run_name='__main__')
                if expected is None:
                    setrlimit.assert_not_called()
                else:
                    limit = expected * 1024 * 1024
                    setrlimit.assert_called_once_with(
                        resource.RLIMIT_AS, (limit if hard == resource.RLIM_INFINITY else min(limit, hard), hard))

    def test_finite_hard_limit_caps_the_memory_limit(self):
        import resource
        hard = 600 * 1024 * 1024
        with patch('resource.getrlimit', return_value=(hard, hard)), patch('resource.setrlimit') as setrlimit:
            self.assertIsNone(self.run_bootstrap())
        setrlimit.assert_called_once_with(resource.RLIMIT_AS, (hard, hard))

    def test_platform_without_resource_module_runs_without_a_limit(self):
        with patch.dict(sys.modules, {'resource': None}):
            self.assertIsNone(self.run_bootstrap())
        self.assertEqual(json.loads((self.directory / 'progress.json').read_text())['stage'], 'start')
        self.assertFalse((self.directory / 'error.json').exists())


@tag('docx')
class EpubDocumentTests(WorkerDirectoryMixin, SimpleTestCase):
    """Pełna konwersja DOCX → EPUB w tym procesie."""

    def epub(self, document):
        """{nazwa pliku rozdziału: drzewo XHTML}."""
        source = self.directory / 'source.docx'
        source.write_bytes(document.getvalue())
        convert(source, self.directory, ['epub'], 'Test')
        with ZipFile(self.directory / 'document.epub') as archive:
            return {Path(name).name: etree.fromstring(archive.read(name)) for name in archive.namelist()
                    if name.endswith('.xhtml') and not name.endswith('nav.xhtml')}

    def test_alignment_survives_footnotes_soft_breaks_and_insertions(self):
        """Regresja: akapit z przypisem, miękkim enterem lub wstawką śledzonych zmian tracił wyrównanie."""
        doc = Document()
        noted = doc.add_paragraph('Z przypisem')
        noted.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        add_footnote_reference(noted)
        broken = doc.add_paragraph('Pierwsza linia')
        broken.runs[0].add_break()
        broken.add_run('druga linia')
        broken.alignment = WD_ALIGN_PARAGRAPH.CENTER
        inserted = doc.add_paragraph('Przed ')
        add_tracked_insertion(inserted, 'wstawką')
        inserted.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        (content,) = self.epub(with_footnote(doc, 'Przypis')).values()
        classes = {' '.join(''.join(p.itertext()).split()): p.get('class') for p in content.iter(XHTML + 'p')}
        self.assertEqual(classes['Z przypisem[1]'], 'align-right')
        self.assertEqual(classes['Pierwsza liniadruga linia'], 'align-center')
        self.assertEqual(classes['Przed wstawką'], 'align-justify')

    def test_footnote_links_lead_to_the_chapter_with_the_note(self):
        """Regresja: po podziale na rozdziały odnośniki przypisów wskazywały kotwice w innym pliku."""
        doc = Document()
        for number in (1, 2):
            doc.add_heading(f'Rozdział {number}', 1)
            doc.add_paragraph(f'Treść {number}')
        add_footnote_reference(doc.paragraphs[1])
        chapters = self.epub(with_footnote(doc, 'Przypis'))
        self.assertEqual(sorted(chapters), ['chapter-001.xhtml', 'chapter-002.xhtml'])
        ids = {name: set(tree.xpath('//@id')) for name, tree in chapters.items()}
        links = [(name, href) for name, tree in chapters.items() for href in tree.xpath('//@href')
                 if '#' in href and not href.endswith('.css')]
        self.assertIn(('chapter-001.xhtml', 'chapter-002.xhtml#footnote-1'), links)
        self.assertIn(('chapter-002.xhtml', 'chapter-001.xhtml#footnote-ref-1'), links)
        for name, href in links:
            target, anchor = href.split('#')
            self.assertIn(anchor, ids[target or name], href)


class PageBreakMarkupTests(SimpleTestCase):
    """Separatory scen i znaczniki podziału strony w HTML z Mammotha (bez konwersji pliku)."""

    def test_scene_break_paragraphs(self):
        for text in ('*', '* * *', '***', '#', '# # #', '⁂', '~', '~ ~ ~', '— — —', '- - -', '• • •', '§ § §'):
            with self.subTest(text=text):
                self.assertTrue(scene_break(html.fromstring(f'<p>{text}</p>')))
        # Samotna kreska, punktor czy paragraf to zwykle urwana kwestia, pusty punkt albo odsyłacz, nie separator.
        for markup in ('<p>* Przypis</p>', '<p>___</p>', '<p>== ==</p>', '<p></p>', '<p> </p>', '<h2>***</h2>',
                       '<p>* * * * * * * * * *</p>', '<p>*<img src="a.png"></p>', '<p>1.</p>', '<p>—</p>',
                       '<p>–</p>', '<p>-</p>', '<p> • </p>', '<p>§</p>', '<p>·</p>'):
            with self.subTest(markup=markup):
                self.assertFalse(scene_break(html.fromstring(markup)))

    def test_only_the_page_break_marker_class_survives_sanitizing(self):
        content = sanitize_html('<p>a<br class="page-break">b<br class="inna">c<span class="page-break">d</span></p>', {})
        self.assertEqual(content, '<p>a<br class="page-break">b<br>c<span>d</span></p>')

    def test_split_keeps_formatting_and_unique_ids(self):
        root = html.fragment_fromstring(
            '<p id="a" class="align-center">A<em id="e">b<br class="page-break">c</em>d</p><table><tr><td>'
            'T<br class="page-break">U</td></tr></table><p>Dalej</p>', create_parent='div')
        split_page_breaks(root)
        self.assertEqual([html.tostring(child, encoding='unicode') for child in root], [
            '<p id="a" class="align-center">A<em id="e">b</em></p>',
            '<p class="align-center" data-page-break="1"><em>c</em>d</p>',
            '<table><tr><td>T<br>U</td></tr></table>',  # w tabeli podział to zwykły koniec wiersza
            '<p>Dalej</p>',
        ])

    def test_heading_after_a_break_keeps_its_anchor_and_breaks_add_up(self):
        root = html.fragment_fromstring(
            '<p>A</p><h1 id="section-1"><a id="_Toc1"></a><br class="page-break">Rozdział</h1>'
            '<p><br class="page-break"></p><p>Po<br class="page-break"></p><p><br class="page-break"></p><p>B</p>'
            '<ul><li><p>Punkt<br class="page-break">dalej</p></li></ul>', create_parent='div')
        split_page_breaks(root)
        self.assertEqual([html.tostring(child, encoding='unicode') for child in root], [
            '<p>A</p>',
            '<h1 id="section-1" data-page-break="1"><a id="_Toc1"></a>Rozdział</h1>',
            '<p data-page-break="1">Po</p>',
            # Dwa podziały z rzędu: w PDF zostaje pusta strona.
            '<p data-page-break="2">B</p>',
            '<ul><li><p>Punkt<br>dalej</p></li></ul>',
        ])

    def test_markup_or_list_after_a_break_moves_to_the_next_block(self):
        cases = (
            ('znaczniki po podziale', '<p>A<br class="page-break"><strong>B</strong> C</p>',
             ['<p>A</p>', '<p data-page-break="1"><strong>B</strong> C</p>']),
            ('lista po podziale', '<p>Koniec<br class="page-break"></p><ul><li>Punkt</li></ul>',
             ['<p>Koniec</p>', '<ul data-page-break="1"><li>Punkt</li></ul>']),
        )
        for label, markup, expected in cases:
            with self.subTest(label):
                root = html.fragment_fromstring(markup, create_parent='div')
                split_page_breaks(root)
                self.assertEqual([html.tostring(child, encoding='unicode') for child in root], expected)


@tag('slow', 'docx')
class PageBreakAndSceneBreakTests(WorkerDirectoryMixin, SimpleTestCase):
    """Podziały stron Worda i separatory scen: odstęp w EPUB, nowa strona w PDF."""

    def epub(self, document):
        source = self.directory / 'source.docx'
        document.save(source)
        convert(source, self.directory, ['epub'], 'Test')
        with ZipFile(self.directory / 'document.epub') as archive:
            names = sorted(name for name in archive.namelist() if name.endswith('.xhtml') and 'nav' not in name)
            css = archive.read('EPUB/style.css').decode()
            return [etree.fromstring(archive.read(name)) for name in names], css

    def test_epub_page_breaks_are_gaps_not_chapters(self):
        doc = Document()
        add_page_break(doc.add_paragraph())  # podział przed treścią nie daje odstępu
        add_page_break(doc.add_paragraph('Przed'), 'Po')
        add_page_break(doc.add_paragraph())
        doc.add_paragraph('Nowa strona').paragraph_format.page_break_before = True
        doc.add_paragraph('Koniec')
        (chapter,), css = self.epub(doc)
        body = chapter.find(XHTML + 'body')
        self.assertEqual([(child.tag[len(XHTML):], child.get('class'), ''.join(child.itertext())) for child in body], [
            ('p', 'align-left', 'Przed'), ('hr', 'page-break', ''), ('p', 'align-left', 'Po'),
            ('hr', 'page-break', ''), ('p', 'align-left', 'Nowa strona'), ('p', 'align-left', 'Koniec')])
        self.assertIn('hr.page-break', css)
        self.assertFalse(chapter.xpath('//@data-page-break'))

    def test_page_break_before_a_chapter_is_dropped_from_the_previous_one(self):
        doc = Document()
        for number in (1, 2):
            doc.add_heading(f'Rozdział {number}', 1)
            add_page_break(doc.add_paragraph(f'Treść {number}'))
        chapters, _ = self.epub(doc)
        self.assertEqual(len(chapters), 2)
        self.assertFalse([tree for tree in chapters if tree.xpath('//x:hr', namespaces={'x': XHTML[1:-1]})])

    def test_chapters_opened_with_a_page_or_column_break_stay_in_the_table_of_contents(self):
        """Regresja (realna książka): rozdział zaczynający się podziałem w akapicie Nagłówka 1 znikał ze spisu."""
        doc = Document()
        doc.add_paragraph('Strona tytułowa')
        for number, kind in enumerate(('page', 'column', 'page'), 1):
            add_leading_break(doc.add_heading(f'Rozdział {number}', 1), kind)
            doc.add_paragraph(f'Treść {number}')
        chapters, _ = self.epub(doc)
        with ZipFile(self.directory / 'document.epub') as archive:
            nav = etree.fromstring(archive.read('EPUB/nav.xhtml'))
            ncx = etree.fromstring(archive.read('EPUB/toc.ncx'))
        links = nav.xpath('//x:nav//x:a', namespaces={'x': XHTML[1:-1]})
        self.assertEqual([link.text for link in links], ['Rozdział 1', 'Rozdział 2', 'Rozdział 3'])
        self.assertEqual(len(ncx.xpath('//n:navPoint', namespaces={'n': 'http://www.daisy.org/z3986/2005/ncx/'})), 3)
        ids = [tree.xpath('//@id') for tree in chapters]
        for link in links:
            name, anchor = link.get('href').split('#')
            self.assertIn(anchor, ids[int(name[8:11]) - 1])
        self.assertFalse((self.directory / 'warnings.json').exists())  # podział kolumny nie jest „nieobsługiwany”

    def test_table_of_contents_skips_empty_headings_and_footnote_numbers(self):
        doc = Document()
        add_footnote_reference(doc.add_heading('Rozdział 1', 1))
        doc.add_paragraph('Treść 1')
        doc.add_heading('', 1)
        doc.add_paragraph('Treść 2')
        doc.add_heading('Rozdział 3', 1)
        doc.add_paragraph('Treść 3')
        self.epub(Document(with_footnote(doc, 'Przypis')))
        with ZipFile(self.directory / 'document.epub') as archive:
            nav = etree.fromstring(archive.read('EPUB/nav.xhtml'))
            ncx = etree.fromstring(archive.read('EPUB/toc.ncx'))
        self.assertEqual([''.join(a.itertext()) for a in nav.xpath('//x:nav//x:a', namespaces={'x': XHTML[1:-1]})],
                         ['Rozdział 1', 'Rozdział 3'])
        self.assertEqual(ncx.xpath('//n:navLabel/n:text/text()',
                                   namespaces={'n': 'http://www.daisy.org/z3986/2005/ncx/'}),
                         ['Rozdział 1', 'Rozdział 3'])

    def test_epub_scene_break_is_centred_and_keeps_its_text(self):
        doc = Document()
        doc.add_paragraph('Scena pierwsza.')
        doc.add_paragraph('* * *').alignment = WD_ALIGN_PARAGRAPH.LEFT
        doc.add_paragraph('Scena druga.')
        (chapter,), css = self.epub(doc)
        separators = chapter.xpath('//x:p[@class="scene-break"]', namespaces={'x': XHTML[1:-1]})
        self.assertEqual([''.join(p.itertext()) for p in separators], ['* * *'])
        self.assertRegex(css, r'\.scene-break \{ text-align: center; text-indent: 0; margin: [^0]')

    def test_headers_and_footers_stay_out_of_the_epub_without_a_warning(self):
        doc = Document()
        doc.sections[0].header.paragraphs[0].text = 'Żywa pagina'
        doc.sections[0].footer.paragraphs[0].text = 'Stopka'
        doc.add_paragraph('Treść')
        (chapter,), _ = self.epub(doc)
        self.assertNotIn('pagina', etree.tostring(chapter, encoding='unicode'))
        self.assertNotIn('Stopka', etree.tostring(chapter, encoding='unicode'))
        self.assertFalse((self.directory / 'warnings.json').exists())

    def test_pdf_failure_keeps_the_finished_epub(self):
        doc = Document()
        doc.add_paragraph('Treść')
        source = self.directory / 'source.docx'
        doc.save(source)
        with patch('core.services.document_conversion_worker.render_pdf', side_effect=RuntimeError('tajne')):
            convert(source, self.directory, ['epub', 'pdf'], 'Test')
            self.assertTrue((self.directory / 'document.epub').is_file())
            self.assertFalse((self.directory / 'document.pdf').exists())
            warnings = json.loads((self.directory / 'warnings.json').read_text())
            self.assertIn(PDF_FAILED_NOTE, warnings)
            report = json.loads((self.directory / 'pdf_error.json').read_text())
            self.assertEqual((report['stage'], report['error']), ('PDF', 'RuntimeError'))
            self.assertNotIn('tajne', json.dumps(report))
            # Sam PDF bez e-booka: błąd idzie dalej, jak dotąd.
            with self.assertRaises(RuntimeError):
                convert(source, self.directory, ['pdf'], 'Test')

    def test_pdf_starts_a_new_page_at_word_page_breaks(self):
        doc = Document()
        add_page_break(doc.add_paragraph('Pierwsza'), 'Druga')
        add_page_break(doc.add_paragraph())
        doc.add_paragraph('Trzecia')
        source = self.directory / 'source.docx'
        doc.save(source)
        convert(source, self.directory, ['pdf'], 'Test')
        pdf = (self.directory / 'document.pdf').read_bytes()
        self.assertEqual(len(re.findall(rb'/Type /Page\b(?!s)', pdf)), 3)
        self.assertFalse((self.directory / 'warnings.json').exists())


@tag('docx')
class HeadingConversionTests(WorkerDirectoryMixin, SimpleTestCase):
    """Nagłówki w e-booku i konspekcie PDF według reguły nagłówków (także polskie nazwy stylów)."""

    def test_polish_heading_names_make_chapters_and_the_pdf_outline(self):
        """Regresja: styl „Nagłówek 1” (polska nazwa zamiast wbudowanej „heading 1”) był w e-booku zwykłym
        akapitem – bez rozdziałów, spisu treści i konspektu PDF."""
        source = self.directory / 'source.docx'
        source.write_bytes(localized_style_names().getvalue())
        sections, original = [], FPDF.start_section

        def section_spy(pdf, name, level=0, strict=True):
            sections.append((name, level))
            return original(pdf, name, level, strict)
        with patch.object(FPDF, 'start_section', section_spy):
            convert(source, self.directory, ['epub', 'pdf'], 'Książka')
        with ZipFile(self.directory / 'document.epub') as epub:
            chapters = sorted(name for name in epub.namelist() if name.startswith('EPUB/chapter-'))
            second = epub.read(chapters[1]).decode('utf-8')
        self.assertEqual(len(chapters), 3)  # strona tytułowa i dwa rozdziały
        self.assertIn('<h1 id="section-1" class="align-left">Rozdział pierwszy</h1>', second)
        self.assertIn('<h2 id="section-2" class="align-left">Podrozdział</h2>', second)
        self.assertEqual(sections, [('Rozdział pierwszy', 0), ('Podrozdział', 1), ('Rozdział drugi', 0)])

    def test_heading_tags_and_pdf_outline_follow_the_outline_rule(self):
        """h1–h6 w e-booku (i konspekt PDF z h1–h3) dokładnie wtedy, gdy reguła nagłówków daje poziom 1–6
        (OUTLINE_CASES, do tego styl Nagwek2 bez definicji): także styl oparty na nagłówku, styl „Rozdział” i akapit
        z poziomem konspektu, „Naglowek 1”; „Heading” bez numeru i nagłówek z poziomem 9 to zwykły akapit.
        Styl pochodny w kopii dla Mammotha nie zmienia formatowania (wyśrodkowanie ze stylu „Rozdział”)."""
        document = outline_document()
        document.styles['Rozdział'].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        undefined = document.add_paragraph('styl Nagwek2 bez definicji')
        undefined._p.insert(0, parse_xml(f'<w:pPr {nsdecls("w")}><w:pStyle w:val="Nagwek2"/></w:pPr>'))
        levels = {text: level for text, (_, _, level, _) in OUTLINE_CASES.items()} | {undefined.text: 2}
        source = self.directory / 'source.docx'
        document.save(source)
        sections, original = [], FPDF.start_section

        def section_spy(pdf, name, level=0, strict=True):
            sections.append((name, level))
            return original(pdf, name, level, strict)
        with patch.object(FPDF, 'start_section', section_spy):
            convert(source, self.directory, ['epub', 'pdf'], 'Książka')
        with ZipFile(self.directory / 'document.epub') as epub:
            markup = ''.join(epub.read(name).decode('utf-8') for name in sorted(epub.namelist())
                             if name.startswith('EPUB/chapter-'))
        found = {text: (tag, attributes) for tag, attributes, text in re.findall(r'<(\w+)([^>]*)>([^<]+)</\1>', markup)}
        self.assertEqual({text: found[text][0] for text in levels},
                         {text: f'h{level}' if level else 'p' for text, level in levels.items()})
        self.assertIn('class="align-center"', found['styl Rozdział z poziomem konspektu 0'][1])
        self.assertEqual(sections, [(text, level - 1) for text, level in levels.items() if level and level <= 3])


class ParagraphKeyTests(SimpleTestCase):
    def test_hyphen_characters_are_part_of_the_key(self):
        paragraph = parse_xml(f'<w:p {nsdecls("w")}><w:r><w:t>e</w:t><w:noBreakHyphen/><w:t>mail</w:t>'
                              '<w:softHyphen/><w:t>owy</w:t></w:r></w:p>')
        self.assertEqual(docx_paragraph_key(paragraph), 'e\u2011mail\u00adowy')

    def test_text_box_is_matched_by_its_fallback_copy_only(self):
        document = Document()
        add_text_box(document.add_paragraph('Przed polem.'), 'W polu')
        queues = paragraph_queues(document)
        # Akapit pola tekstowego liczy się raz (kopia w mc:Choice jest pomijana), a akapit z polem – bez jego treści.
        self.assertEqual({key: len(queue) for key, queue in queues.items()}, {'Przed polem.': 1, 'W polu': 1})


class DocumentStoriesTests(SimpleTestCase):
    def test_fallback_paragraphs_are_optional_and_shared_parts_are_read_once(self):
        document = Document()
        add_text_box(document.add_paragraph('Przed polem.'), 'W polu')
        header = document.sections[0].header
        header.paragraphs[0].text = 'Nagłówek'
        # Druga relacja do tej samej części (np. ten sam nagłówek dla stron parzystych).
        document.part.rels.add_relationship(RT.HEADER, header.part, 'rIdShared')
        for include_fallback, expected in ((True, ['Przed polem.', 'W polu', 'W polu']),
                                           (False, ['Przed polem.', 'W polu'])):
            with self.subTest(include_fallback=include_fallback):
                stories, _ = document_stories(document, include_fallback=include_fallback)
                self.assertEqual([story.name for story in stories], ['body', 'header'])
                self.assertEqual([p.text for p in stories[0].paragraphs], expected)
                self.assertEqual([p.text for p in stories[1].paragraphs], ['Nagłówek'])


@tag('docx')
class MammothSourceTests(SimpleTestCase):
    """Kopia DOCX dla Mammotha: treść elementów, które Mammoth pomija albo skleja, jako zwykłe przebiegi; ukryty
    tekst i odsyłacze donikąd usunięte z uwagą; plik bez takich elementów zostaje bez zmian."""

    def html(self, data):
        return mammoth.convert_to_html(BytesIO(data)).value

    def test_markup_mammoth_skips_or_joins_becomes_plain_text(self):
        """Regresja: e-book i PDF gubiły wynik w:fldSimple (numer podpisu z Worda, pole z OnlyOffice) i tekst
        w w:dir/w:bdo, sklejały słowa wokół w:cr i w:ptab, a inicjał (w:dropCap) był osobnym akapitem."""
        ptab = '<w:r><w:ptab w:relativeTo="margin" w:alignment="right" w:leader="none"/></w:r>'
        cases = {
            'pole proste': (p(r('Rysunek '), '<w:fldSimple w:instr=" SEQ Rysunek "><w:fldData>AA==</w:fldData>'
                              + r('1') + '</w:fldSimple>'), '<p>Rysunek 1</p>'),
            'kierunek tekstu': (p('<w:dir w:val="rtl">' + r('עברית') + '</w:dir>', r(' i '),
                                  '<w:bdo w:val="ltr">' + r('LTR') + '</w:bdo>'), '<p>עברית i LTR</p>'),
            'powrót karetki': (p(r('Przed'), '<w:r><w:cr/></w:r>', r('po')), '<p>Przed<br />po</p>'),
            'tabulator położenia': (p(r('Lewa'), ptab, r('prawa')), '<p>Lewa\tprawa</p>'),
            'tag inteligentny': (p('<w:smartTag w:element="City"><w:smartTagPr><w:attr w:name="a" w:val="b"/>'
                                   '</w:smartTagPr>' + r('Warszawa') + '</w:smartTag>'), '<p>Warszawa</p>'),
        }
        for label, (body, expected) in cases.items():
            with self.subTest(label):
                data, notes = mammoth_source(word_docx(body).getvalue())
                self.assertEqual(self.html(data), expected)
                self.assertEqual(notes, [])
        data, _ = mammoth_source(frames_and_drop_cap().getvalue())
        self.assertTrue(self.html(data).startswith('<p>Wielki inicjał zaczyna ten akapit.</p>'))

    def test_hidden_runs_and_dangling_note_references_are_dropped_with_notes(self):
        """Tekst ukryty (własne w:vanish i styl znakowy z w:vanish) nie trafia do e-booka – jak przy wydruku;
        w:vanish="0" i znaczniki pól zostają. Odsyłacz do przypisu, którego nie ma, przerywał konwersję."""
        styles = STYLES + ('<w:style w:type="character" w:styleId="Ukryty"><w:name w:val="Ukryty"/><w:rPr><w:vanish/>'
                           '</w:rPr></w:style>')
        hidden = '<w:rPr><w:vanish/></w:rPr>'
        index_entry = (f'<w:r>{hidden}<w:fldChar w:fldCharType="begin"/></w:r><w:r>{hidden}<w:instrText> XE "kot" '
                       f'</w:instrText></w:r><w:r>{hidden}<w:fldChar w:fldCharType="end"/></w:r>')
        body = p(r('Widać '), r('ukryte ', '<w:vanish/>'), r('stylem ', '<w:rStyle w:val="Ukryty"/>'),
                 r('jawne', '<w:vanish w:val="0"/>'), index_entry, note_reference('footnote', 1),
                 note_reference('footnote', 9), r('.'))
        footnotes = notes_part('footnote', {1: r(' Przypis.')})
        source = word_docx(body, styles=styles, parts={'word/footnotes.xml': footnotes},
                           relationships=[('rIdFoot', f'{R_NS}/footnotes', 'footnotes.xml')],
                           overrides=[('/word/footnotes.xml', WORD_PART.format('footnotes'))])
        data, notes = mammoth_source(source.getvalue())
        html = self.html(data)
        # Odsyłacz w przebiegu z indeksem górnym: Mammoth dokłada własny <sup>.
        self.assertTrue(html.startswith('<p>Widać jawne<sup><sup><a href="#footnote-1" id="footnote-ref-1">[1]</a>'
                                        '</sup></sup>.</p>'), html)
        for hidden_text in ('ukryte', 'stylem', 'footnote-9'):
            self.assertNotIn(hidden_text, html)
        self.assertEqual(notes, [MAMMOTH_NOTES['missing_note'], MAMMOTH_NOTES['hidden'] + ' (3 razy)'])

    def test_charts_are_reported_and_plain_documents_stay_unchanged(self):
        """Wykres i SmartArt Mammoth pomija bez ostrzeżenia – uwaga mówi o nich użytkownikowi; dokument bez
        poprawianych elementów wraca bajt w bajt."""
        charts = chart_and_smart_art().getvalue()
        self.assertEqual(mammoth_source(charts), (charts, [MAMMOTH_NOTES['graphic'] + ' (2 razy)']))
        plain = word_docx(p(r('Zwykły tekst.'))).getvalue()
        self.assertEqual(mammoth_source(plain), (plain, []))


@tag('docx')
class ScriptModeWorkerTests(WorkerDirectoryMixin, SimpleTestCase):
    def run_job(self, document, **config):
        """Uruchom bootstrap jako __main__; zwraca kod wyjścia procesu roboczego."""
        document.save(self.directory / 'source.docx')
        # memory_limit_mb = 0: bez limitu RLIMIT_AS, który dotyczyłby całego procesu testów.
        (self.directory / 'job.json').write_text(json.dumps({'title': 'Skrypt', 'memory_limit_mb': 0, **config}),
                                                 encoding='utf-8')
        with script_mode(self.directory), self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(BOOTSTRAP), run_name='__main__')
        return caught.exception.code

    @tag('slow')
    def test_full_job_runs_every_step_without_the_package(self):
        """Odkurzacz, miękkie entery, ujednolicenie, powtórzenia, EPUB i PDF – wszystko z importów skryptu."""
        document = Document()
        paragraph = document.add_paragraph('Kot  i kot "siedzą" obok')
        paragraph.add_run().add_break()
        paragraph.add_run('drugiej linii.')
        code = self.run_job(document, formats=['epub', 'pdf'], prepare=True, clean=True, cleaner_rules=None,
                            remove_soft_whitespace=True, include_docx=True, cleaner_summary=True,
                            repetitions={'min_word_length': 3, 'analysis_options': {
                                'long_sentences': False, 'long_paragraphs': False}})
        self.assertEqual(code, 0)
        self.assertFalse((self.directory / 'error.json').exists())
        marked = Document(self.directory / 'source.docx')
        # Miękki enter dzieli akapit; reguły domyślne poprawiają spacje i cudzysłowy.
        self.assertEqual([p.text for p in marked.paragraphs], ['Kot i kot „siedzą” obok', 'drugiej linii.'])
        self.assertTrue(any(run.font.color.rgb for run in marked.paragraphs[0].runs))
        self.assertGreater(json.loads((self.directory / 'summary.json').read_text(encoding='utf-8'))
                           ['spaces'], 0)
        with ZipFile(self.directory / 'document.epub') as epub:
            self.assertIn('drugiej linii.', epub.read('EPUB/content.xhtml').decode())
        self.assertTrue((self.directory / 'document.pdf').read_bytes().startswith(b'%PDF'))
        self.assertEqual(json.loads((self.directory / 'images.json').read_text()), {'removed_images': 0})

    def test_inspection_and_public_errors_without_the_package(self):
        """Inspekcja przebudowy (z formatem strony) kończy się sukcesem, a błąd dokumentu ma publiczny kod."""
        self.assertEqual(self.run_job(Document(), formats=[], inspect=True, keep_page_format=True), 0)
        document = Document()
        document.add_paragraph('a' * 20_001)
        self.assertEqual(self.run_job(document, formats=['epub'], prepare=True, clean=True, cleaner_rules=[]), 3)
        report = json.loads((self.directory / 'error.json').read_text(encoding='utf-8'))
        self.assertEqual((report['error'], report['public_code']), ('DocumentInputError', 'cleaner_paragraph_limit'))
