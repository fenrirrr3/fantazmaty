"""Proces roboczy konwertera (uruchamiany w tym procesie): bezpieczny HTML, akapity list, rozdziały i obrazy EPUB,
uwagi po polsku oraz raport błędu bez treści dokumentu."""
import json
import runpy
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase
from docx import Document
from lxml import html
from PIL import Image

import core.services
from core.services import document_progress
from core.services.document_conversion_worker import convert, main, polish_warnings, sanitize_html, split_chapters
from core.services.document_html import list_paragraphs

from .support import png


class SanitizedHtmlTests(SimpleTestCase):
    def test_only_allowed_tags_attributes_and_links_survive(self):
        content, headings = sanitize_html(
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
        self.assertEqual(headings, [('section-1', 'Tytuł'), ('wlasny', 'Rozdział')])

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


class EmbeddedImageTests(SimpleTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def source(self, picture):
        document = Document()
        document.add_paragraph('Ilustracja')
        document.add_picture(picture)
        path = self.directory / 'source.docx'
        document.save(path)
        return path

    def test_images_are_rasterized_into_epub_and_pdf(self):
        convert(self.source(png(20, 10)), self.directory, ['epub', 'pdf'], 'Obraz')
        with ZipFile(self.directory / 'document.epub') as epub:
            self.assertIn('EPUB/assets/image-1.png', epub.namelist())
            self.assertIn('src="assets/image-1.png"', epub.read('EPUB/content.xhtml').decode())
        self.assertIn(b'/Subtype /Image', (self.directory / 'document.pdf').read_bytes())

    def test_only_raster_formats_are_embedded(self):
        """Obraz w innym formacie (np. ICO podpisane jako PNG) nie trafia do e-booka."""
        source = self.source(png(20, 10))
        icon = BytesIO()
        Image.new('RGB', (16, 16)).save(icon, 'ICO')
        packed = self.directory / 'packed.docx'
        with ZipFile(source) as original, ZipFile(packed, 'w') as target:
            for item in original.infolist():
                data = original.read(item)
                target.writestr(item, icon.getvalue() if item.filename.startswith('word/media/') else data)
        with self.assertRaisesMessage(ValueError, 'Unsupported image'):
            convert(packed, self.directory, ['epub'], 'Obraz')

    def test_image_with_huge_dimensions_is_refused(self):
        with self.assertRaisesMessage(ValueError, 'Image dimensions too large'):
            convert(self.source(png(5000, 5000)), self.directory, ['epub'], 'Obraz')


class WorkerMainTests(SimpleTestCase):
    """Wywołanie main() tak jak bootstrap procesu: katalog zadania w argv, wynik w plikach katalogu."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # main() kieruje raport postępu do katalogu zadania; po teście wyłączamy go.
        progress = patch.object(document_progress, '_target', None)
        progress.start()
        self.addCleanup(progress.stop)

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

    def test_repetitions_replace_the_source_with_the_marked_document(self):
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

    def test_bootstrap_records_failures_before_worker_initialization(self):
        """Regresja: błąd importu przed startem procesu roboczego ujawniał treść wyjątku."""
        bootstrap = Path(core.services.__file__).with_name('document_worker_bootstrap.py')
        runner = runpy.run_path
        with patch('sys.argv', ['bootstrap', str(self.directory)]), \
                patch('runpy.run_path', side_effect=ImportError('PRIVATE credential')), \
                self.assertRaises(SystemExit) as caught:
            runner(str(bootstrap), run_name='__main__')
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.report()['error'], 'ImportError')
        self.assertNotIn('PRIVATE', json.dumps(self.report()))

    def test_missing_library_exits_with_code_two(self):
        with patch('core.services.document_conversion_worker.convert', side_effect=ImportError('tajne')):
            self.assertEqual(self.run_worker(self.document(), formats=['pdf'], prepare=True), 2)
        self.assertEqual(self.report()['error'], 'ImportError')
        self.assertNotIn('tajne', json.dumps(self.report()))
