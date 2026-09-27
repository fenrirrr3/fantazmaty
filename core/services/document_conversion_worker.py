"""Isolated Python worker: DOCX -> Mammoth HTML -> EPUB/PDF, no office suite."""
import html as escape_html
import json
import sys
import traceback
from importlib.metadata import version, PackageNotFoundError
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

CURRENT_STAGE = 'DOCX'

CSS = '''
body { font-family: "Times New Roman", serif; font-size: 12pt; line-height: 1.5; }
p, h1, h2, h3, h4, h5, h6 { margin: 0; font-size: 12pt; line-height: 1.5; text-indent: 1.25cm; orphans: 3; widows: 3; }
.align-center { text-align: center; text-indent: 0; }
.align-right { text-align: right; }
.align-justify { text-align: justify; }
.align-left { text-align: left; }
h1, h2, h3, h4, h5, h6 { break-after: avoid; }
p:empty { min-height: 1.5em; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #aaa; padding: .3em; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; }
'''
TAGS = set('p h1 h2 h3 h4 h5 h6 strong em u s del sup sub table thead tbody tfoot tr td th caption blockquote ol ul li br hr span a img pre code'.split())


def sanitize_html(fragment, assets):
    from lxml import html
    root = html.fragment_fromstring(fragment or '<p></p>', create_parent='div')
    for element in list(root.iterdescendants()):
        if not isinstance(element.tag, str):
            element.drop_tree(); continue
        if element.tag not in TAGS:
            element.drop_tag(); continue
        for key, value in list(element.attrib.items()):
            allowed = key in ('id', 'title')
            if element.tag == 'a' and key == 'href':
                allowed = value.startswith('#') or urlsplit(value).scheme.lower() in ('http', 'https', 'mailto')
            if element.tag == 'img' and key == 'src':
                allowed = value in assets
            if element.tag == 'img' and key == 'alt':
                allowed = True
            if element.tag in ('td', 'th') and key in ('colspan', 'rowspan'):
                allowed = value.isascii() and value.isdecimal() and 1 <= int(value) <= 100
            if not allowed:
                del element.attrib[key]
    headings = []
    for number, element in enumerate(root.xpath('.//h1 | .//h2 | .//h3'), 1):
        if not element.get('id'):
            element.set('id', 'section-' + str(number))
        headings.append((element.get('id'), element.text_content()[:200]))
    # Serialize children as HTML; EbookLib normalizes the chapter to XHTML.
    content = (escape_html.escape(root.text) if root.text else '') + ''.join(html.tostring(child, encoding='unicode') for child in root)
    return content, headings


def convert(source, directory, formats, title):
    global CURRENT_STAGE
    CURRENT_STAGE = 'DOCX'
    import mammoth
    from PIL import Image
    assets = {}
    total_image_bytes = 0

    def convert_image(image):
        nonlocal total_image_bytes
        with image.open() as stream:
            data = stream.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('Image too large')
        with Image.open(BytesIO(data)) as picture:
            if picture.width * picture.height > 20_000_000:
                raise ValueError('Image dimensions too large')
            # Rasterize only known image formats, never embed SVG scripts/URLs.
            if picture.format not in ('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP', 'TIFF'):
                raise ValueError('Unsupported image')
            picture.load()
            rendered = BytesIO()
            picture.convert('RGBA').save(rendered, format='PNG')
        data = rendered.getvalue()
        total_image_bytes += len(data)
        if total_image_bytes > 30 * 1024 * 1024:
            raise ValueError('Too many images')
        name = f'assets/image-{len(assets) + 1}.png'
        assets[name] = data
        return {'src': name}

    with source.open('rb') as document:
        result = mammoth.convert_to_html(document,
            convert_image=mammoth.images.img_element(convert_image),
            external_file_access=False, include_embedded_style_map=False, style_map='u => u',
            ignore_empty_paragraphs=False)
    if any(message.type == 'error' for message in result.messages):
        raise ValueError('Document could not be read completely')
    content, headings = sanitize_html(result.value, assets)
    # Mammoth deliberately omits paragraph geometry. Restore alignment from DOCX
    # after sanitization; only our own allowlisted classes reach the EPUB.
    from collections import defaultdict, deque
    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from lxml import html
    document = Document(source)
    paragraphs = defaultdict(deque)
    normalize = lambda value: ' '.join(value.split())
    for element in document.element.body.iter(qn('w:p')):
        paragraph = Paragraph(element, document)
        if paragraph.text.strip():
            paragraphs[normalize(paragraph.text)].append(paragraph)
    root = html.fragment_fromstring(content or '<p></p>', create_parent='div')
    for node in root.iterdescendants():
        if node.tag not in ('p','h1','h2','h3','h4','h5','h6'):
            continue
        matches = paragraphs[normalize(node.text_content())]
        if matches:
            paragraph = matches.popleft()
            alignment = paragraph.alignment
            style = paragraph.style
            while alignment is None and style is not None:
                alignment = style.paragraph_format.alignment
                style = style.base_style
            node.set('class', {0:'align-left',1:'align-center',2:'align-right',3:'align-justify'}.get(alignment, 'align-left'))
    for node in root.iter('p'):
        if not node.text_content() and len(node) == 0:
            node.text = '\u00a0'
    content = ''.join(html.tostring(child, encoding='unicode') for child in root)

    if 'epub' in formats:
        CURRENT_STAGE = 'EPUB'
        from ebooklib import epub
        book = epub.EpubBook()
        book.set_identifier(str(uuid4()))
        book.set_title(title); book.set_language('pl')
        chapter = epub.EpubHtml(title=title, file_name='content.xhtml', lang='pl')
        chapter.content = content
        stylesheet = epub.EpubItem(uid='style', file_name='style.css', media_type='text/css', content=CSS.encode())
        book.add_item(stylesheet); chapter.add_item(stylesheet); book.add_item(chapter)
        for number, (name, data) in enumerate(assets.items()):
            book.add_item(epub.EpubItem(uid=f'image-{number}', file_name=name, media_type='image/png', content=data))
        book.toc = tuple(epub.Link('content.xhtml#' + id_, label, 'toc-' + str(n)) for n, (id_, label) in enumerate(headings)) or (chapter,)
        book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
        book.spine = ['nav', chapter]
        epub.write_epub(str(directory / 'document.epub'), book, {'raise_exceptions': True})
    if 'pdf' in formats:
        CURRENT_STAGE = 'PDF'
        if __package__:
            from .document_pdf import render_pdf
        else:
            from document_pdf import render_pdf
        render_pdf(source, directory / 'document.pdf', content, assets, title)


def main():
    directory = Path(sys.argv[1])
    config = json.loads((directory / 'job.json').read_text(encoding='utf-8'))
    formats = config['formats']
    if (not formats and not config.get('include_docx') and not config.get('inspect')) or set(formats) - {'pdf', 'epub'}:
        return 3
    try:
        if config.get('prepare') or config.get('inspect'):
            if __package__:
                from .document_preparation import prepare_docx, ALL_EDITORIAL_RULES
                from .document_rebuild import inspect_docx
            else:
                from document_preparation import prepare_docx, ALL_EDITORIAL_RULES
                from document_rebuild import inspect_docx
            source = directory / 'source.docx'
            if config.get('inspect'):
                with source.open('rb') as document:
                    inspect_docx(document)
                return 0
            rules = config.get('cleaner_rules') if config.get('clean') else ()
            if rules is None:
                rules = list(ALL_EDITORIAL_RULES)
            with source.open('rb') as document, prepare_docx(
                document, rebuild=config.get('rebuild', False),
                normalize_formatting=config.get('normalize', True),
                cleaner_rules=rules, use_cleaner=config.get('clean', False),
                allow_omissions=config.get('allow_rebuild_omissions', False),
            ) as prepared:
                payload = prepared.read()
            source.write_bytes(payload)
        if formats:
            convert(directory / 'source.docx', directory, formats, config['title'])
    except Exception as error:
        # Never record exception messages, locals, source lines or document text.
        versions = {}
        for package in ('fpdf2', 'fpdf', 'fonttools', 'mammoth', 'EbookLib', 'python-docx', 'Pillow'):
            try: versions[package] = version(package)
            except PackageNotFoundError: versions[package] = 'not installed'
        frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        report = {'stage': CURRENT_STAGE, 'error': type(error).__name__,
                  'omissions': getattr(error, 'omissions', []), 'frames': frames, 'python': sys.version.split()[0], 'versions': versions}
        try:
            (directory / 'error.json').write_text(json.dumps(report), encoding='utf-8')
        except OSError:
            pass
        return 2 if isinstance(error, ImportError) else 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
