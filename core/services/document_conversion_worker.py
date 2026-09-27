"""Isolated Python worker: DOCX -> Mammoth HTML -> EPUB/PDF, no office suite."""
import html as escape_html
import json
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

CSS = '''
body { font-family: serif; line-height: 1.5; }
p { margin: 0 0 .6em; orphans: 3; widows: 3; }
h1, h2, h3, h4, h5, h6 { break-after: avoid; }
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
            external_file_access=False, include_embedded_style_map=False)
    if any(message.type == 'error' for message in result.messages):
        raise ValueError('Document could not be read completely')
    content, headings = sanitize_html(result.value, assets)
    if 'epub' in formats:
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
        if __package__:
            from .document_pdf import render_pdf
        else:
            from document_pdf import render_pdf
        render_pdf(source, directory / 'document.pdf', content, assets, title)


def main():
    directory = Path(sys.argv[1])
    config = json.loads((directory / 'job.json').read_text(encoding='utf-8'))
    formats = config['formats']
    if not formats or set(formats) - {'pdf', 'epub'}:
        return 3
    try:
        convert(directory / 'source.docx', directory, formats, config['title'])
    except (ImportError, OSError):
        # Fixed error code; never echo document content or private paths.
        return 2
    except Exception:
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
