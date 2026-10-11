"""Proces roboczy konwertera (uruchamiany w tym procesie): bezpieczny HTML, akapity list, rozdziały i obrazy EPUB,
podziały stron i separatory scen, uwagi po polsku oraz raport błędu bez treści dokumentu."""
import json
import re
import runpy
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase
from docx import Document

from core.services.document_errors import DocumentInputError
from docx.enum.text import WD_ALIGN_PARAGRAPH
from lxml import etree, html
from PIL import Image

import core.services
from core.services import document_progress
from core.services.document_conversion_worker import (PDF_FAILED_NOTE, convert, main, polish_warnings, sanitize_html,
                                                      split_chapters)
from core.services.document_html import list_paragraphs, scene_break, split_page_breaks

from .support import (add_footnote_reference, add_leading_break, add_page_break, add_tracked_insertion, png,
                      with_footnote)


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

    def test_external_links_are_valid_urls(self):
        """Regresja (epubcheck RSC-020): znaki niedozwolone w adresie z Worda psuły walidację EPUB."""
        content, _ = sanitize_html('<a href="http://example.com/a%20b?x=<1>&amp;y=ą z|w">x</a>'
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
        with self.assertRaisesMessage(DocumentInputError, 'nie obsługuje'):
            convert(packed, self.directory, ['epub'], 'Obraz')

    def test_unreadable_image_gets_a_clear_message(self):
        """Regresja: obraz EMF/WMF albo uszkodzony kończył konwersję komunikatem „DOCX: ValueError”."""
        source = self.source(png(20, 10))
        packed = self.directory / 'packed.docx'
        with ZipFile(source) as original, ZipFile(packed, 'w') as target:
            for item in original.infolist():
                data = original.read(item)
                target.writestr(item, b'\x01\x00\x00\x00EMF?' if item.filename.startswith('word/media/') else data)
        with self.assertRaises(DocumentInputError) as raised:
            convert(packed, self.directory, ['epub'], 'Obraz')
        self.assertEqual(raised.exception.public_code, 'unsupported_image')
        self.assertIn('EMF/WMF', str(raised.exception))

    def test_image_with_huge_dimensions_is_refused(self):
        with self.assertRaisesMessage(DocumentInputError, 'za duży'):
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
                patch('resource.setrlimit'), self.assertRaises(SystemExit) as caught:
            runner(str(bootstrap), run_name='__main__')
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.report()['error'], 'ImportError')
        self.assertNotIn('PRIVATE', json.dumps(self.report()))

    def test_bootstrap_limits_the_worker_memory(self):
        """Mały DOCX z ogromnym XML zjadał gigabajty pamięci serwera: proces dostaje limit przestrzeni adresowej."""
        import resource
        bootstrap = Path(core.services.__file__).with_name('document_worker_bootstrap.py')
        runner = runpy.run_path
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        for config, expected in (({}, 1536), ({'memory_limit_mb': 2048}, 2048), ({'memory_limit_mb': True}, 1536),
                                 ({'memory_limit_mb': 0}, None)):
            (self.directory / 'job.json').write_text(json.dumps(config))
            with self.subTest(config=config), patch('sys.argv', ['bootstrap', str(self.directory)]), \
                    patch('runpy.run_path'), patch('resource.setrlimit') as setrlimit:
                runner(str(bootstrap), run_name='__main__')
                if expected is None:
                    setrlimit.assert_not_called()
                else:
                    limit = expected * 1024 * 1024
                    setrlimit.assert_called_once_with(
                        resource.RLIMIT_AS, (limit if hard == resource.RLIM_INFINITY else min(limit, hard), hard))

    def test_missing_library_exits_with_code_two(self):
        with patch('core.services.document_conversion_worker.convert', side_effect=ImportError('tajne')):
            self.assertEqual(self.run_worker(self.document(), formats=['pdf'], prepare=True), 2)
        self.assertEqual(self.report()['error'], 'ImportError')
        self.assertNotIn('tajne', json.dumps(self.report()))


XHTML = '{http://www.w3.org/1999/xhtml}'


class EpubDocumentTests(SimpleTestCase):
    """Pełna konwersja DOCX → EPUB w tym procesie."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

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


class PageBreakAndSceneBreakTests(SimpleTestCase):
    """Podziały stron Worda i separatory scen: odstęp w EPUB, nowa strona w PDF."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

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
        content, _ = sanitize_html('<p>a<br class="page-break">b<br class="inna">c<span class="page-break">d</span></p>', {})
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
        with patch('core.services.document_pdf.render_pdf', side_effect=RuntimeError('tajne')):
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
