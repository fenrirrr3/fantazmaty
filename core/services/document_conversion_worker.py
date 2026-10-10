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
if __package__:
    from .document_progress import configure, report as report_progress
    from .document_styles import paragraph_property
    from .document_html import list_paragraphs
else:
    from document_progress import configure, report as report_progress
    from document_styles import paragraph_property
    from document_html import list_paragraphs

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
            element.drop_tree()
            continue
        if element.tag not in TAGS:
            element.drop_tag()
            continue
        for key, value in list(element.attrib.items()):
            allowed = key in ("id", "title")
            if element.tag == "a" and key == "href":
                allowed = value.startswith("#") or urlsplit(value).scheme.lower() in (
                    "http",
                    "https",
                    "mailto",
                )
            if element.tag == "img" and key == "src":
                allowed = value in assets
            if element.tag == "img" and key == "alt":
                allowed = True
            if element.tag in ("td", "th") and key in ("colspan", "rowspan"):
                allowed = value.isascii() and value.isdecimal() and 1 <= int(value) <= 100
            if not allowed:
                del element.attrib[key]
    headings = []
    for number, element in enumerate(root.xpath(".//h1 | .//h2 | .//h3"), 1):
        if not element.get("id"):
            element.set("id", "section-" + str(number))
        headings.append((element.get("id"), element.text_content()[:200]))
    # Serialize children as HTML; EbookLib normalizes the chapter to XHTML.
    content = (escape_html.escape(root.text) if root.text else "") + "".join(
        html.tostring(child, encoding="unicode") for child in root
    )
    return content, headings


WARNING_PATTERNS = (
    (r"Unrecognised (paragraph|run|table) style: (.*) \(Style ID: (.*)\)",
     lambda m: "Styl {} „{}” nie ma odpowiednika w e-booku – tekst zachowano w zwykłym formacie.".format(
         {"paragraph": "akapitu", "run": "znaków", "table": "tabeli"}[m[1]], m[2] or m[3])),
    (r"(\w+) style with ID (\S+) was referenced but not defined in the document",
     lambda m: f"Dokument odwołuje się do nieistniejącego stylu ({m[2]}) – pominięto jego formatowanie."),
    (r"A w:sym element with an unsupported character was ignored: char (\S+) in font (.*)",
     lambda m: f"Pominięto symbol specjalny z czcionki {m[2]}."),
    (r"unexpected non-(row|cell) element in table.*",
     lambda m: "Nietypowa budowa tabeli – scalone komórki mogą wyglądać inaczej."),
    (r"Unsupported break type: (.*)", lambda m: f"Pominięto nieobsługiwany podział ({m[1]})."),
    (r"Could not find image file for a:blip element", lambda m: "Nie znaleziono pliku jednego z obrazów – obraz pominięto."),
    (r"Image of type (.*) is unlikely to display in web browsers",
     lambda m: f"Obraz w formacie {m[1]} może się nie wyświetlić w czytniku."),
    (r"A v:imagedata element without a relationship ID was ignored", lambda m: "Pominięto obraz bez źródła."),
    (r"An unrecognised element was ignored: (.*)", lambda m: f"Pominięto nieobsługiwany element dokumentu ({m[1]})."),
    (r"Ignoring complex field .*", lambda m: "Pominięto uszkodzone pole Worda (np. spis treści lub odsyłacz)."),
)


def polish_warnings(messages):
    """Uwagi biblioteki konwertera po polsku, bez powtórzeń, najwyżej 30."""
    import re
    counts = {}
    for message in messages:
        for pattern, render in WARNING_PATTERNS:
            match = re.fullmatch(pattern, message)
            if match:
                text = render(match)
                break
        else:
            text = "Uwaga konwertera: " + message
        counts[text] = counts.get(text, 0) + 1
    return [text if count == 1 else f"{text} ({count} razy)" for text, count in counts.items()][:30]


def split_chapters(content, title):
    """Podziel treść na rozdziały według nagłówków Worda (Nagłówek 1/2/3).

    Rozdziałami są nagłówki najwyższego poziomu, który występuje w tekście
    co najmniej dwa razy (pojedynczy Nagłówek 1 to zwykle tytuł utworu).
    Bez takich nagłówków powstaje jeden rozdział, jak dotąd.
    Zwraca [(tytuł rozdziału, HTML, [(id, etykieta nagłówka)])].
    """
    from lxml import html
    root = html.fragment_fromstring(content or "<p></p>", create_parent="div")
    children = list(root)
    level = next((tag for tag in ("h1", "h2", "h3")
                  if sum(1 for child in children if child.tag == tag) >= 2), None)
    groups = []
    for child in children:
        if level and child.tag == level or not groups:
            groups.append([])
        groups[-1].append(child)
    chapters = []
    for group in groups:
        first = group[0]
        heading = first.text_content().strip()[:200] if first.tag == level else ""
        if not heading and not any(node.text_content().strip() or node.xpath(".//img") for node in group):
            continue  # pusty początek przed pierwszym rozdziałem
        markup = "".join(html.tostring(node, encoding="unicode") for node in group)
        anchors = [(node.get("id"), node.text_content().strip()[:200])
                   for item in group for node in item.xpath("descendant-or-self::*[self::h1 or self::h2 or self::h3]")
                   if node.get("id")]
        chapters.append((heading or title, markup, anchors))
    return chapters or [(title, "<p>\u00a0</p>", [])]


def convert(source, directory, formats, title):
    global CURRENT_STAGE
    CURRENT_STAGE = "DOCX"
    report_progress("Odczytywanie treści DOCX")
    import mammoth
    from PIL import Image

    assets = {}
    total_image_bytes = 0

    def convert_image(image):
        nonlocal total_image_bytes
        with image.open() as stream:
            data = stream.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Image too large")
        with Image.open(BytesIO(data)) as picture:
            if picture.width * picture.height > 20_000_000:
                raise ValueError("Image dimensions too large")
            # Rasterize only known image formats, never embed SVG scripts/URLs.
            if picture.format not in ("PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF"):
                raise ValueError("Unsupported image")
            picture.load()
            rendered = BytesIO()
            picture.convert("RGBA").save(rendered, format="PNG")
        data = rendered.getvalue()
        total_image_bytes += len(data)
        if total_image_bytes > 30 * 1024 * 1024:
            raise ValueError("Too many images")
        name = f"assets/image-{len(assets) + 1}.png"
        assets[name] = data
        return {"src": name}

    with source.open("rb") as document:
        result = mammoth.convert_to_html(
            document,
            convert_image=mammoth.images.img_element(convert_image),
            external_file_access=False,
            include_embedded_style_map=False,
            style_map="u => u",
            ignore_empty_paragraphs=False,
        )
    if any(message.type == "error" for message in result.messages):
        raise ValueError("Document could not be read completely")
    warnings = polish_warnings(
        str(message.message)[:500] for message in result.messages if message.type == "warning"
    )
    if warnings:
        (directory / "warnings.json").write_text(json.dumps(warnings), encoding="utf-8")
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
    def normalize(value):
        return " ".join(value.split())
    for element in document.element.body.iter(qn("w:p")):
        paragraph = Paragraph(element, document)
        if paragraph.text.strip():
            paragraphs[normalize(paragraph.text)].append(paragraph)
    root = html.fragment_fromstring(content or "<p></p>", create_parent="div")
    list_paragraphs(root)
    for node in root.iterdescendants():
        if node.tag not in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            continue
        matches = paragraphs[normalize(node.text_content())]
        if matches:
            paragraph = matches.popleft()
            alignment = paragraph_property(paragraph, "alignment")
            node.set(
                "class",
                {0: "align-left", 1: "align-center", 2: "align-right", 3: "align-justify"}.get(
                    alignment, "align-left"
                ),
            )
    for node in root.iter("p"):
        if not node.text_content() and len(node) == 0:
            node.text = "\u00a0"
    content = "".join(html.tostring(child, encoding="unicode") for child in root)

    if "epub" in formats:
        CURRENT_STAGE = "EPUB"
        report_progress("Tworzenie EPUB")
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier(str(uuid4()))
        book.set_title(title)
        book.set_language("pl")
        stylesheet = epub.EpubItem(
            uid="style", file_name="style.css", media_type="text/css", content=CSS.encode()
        )
        book.add_item(stylesheet)
        chapters, toc = [], []
        parts = split_chapters(content, title)
        for number, (chapter_title, chapter_content, chapter_headings) in enumerate(parts, 1):
            name = "content.xhtml" if len(parts) == 1 else f"chapter-{number:03d}.xhtml"
            chapter = epub.EpubHtml(title=chapter_title, file_name=name, lang="pl")
            chapter.content = chapter_content
            chapter.add_item(stylesheet)
            book.add_item(chapter)
            chapters.append(chapter)
            toc.extend(epub.Link(name + "#" + id_, label, "toc-" + id_) for id_, label in chapter_headings)
        for number, (name, data) in enumerate(assets.items()):
            book.add_item(
                epub.EpubItem(
                    uid=f"image-{number}", file_name=name, media_type="image/png", content=data
                )
            )
        book.toc = tuple(toc) or tuple(chapters)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav", *chapters]
        epub.write_epub(str(directory / "document.epub"), book, {"raise_exceptions": True})
    if "pdf" in formats:
        CURRENT_STAGE = "PDF"
        report_progress("Przygotowanie PDF")
        if __package__:
            from .document_pdf import render_pdf
        else:
            from document_pdf import render_pdf
        render_pdf(source, directory / "document.pdf", content, assets, title)


def main():
    global CURRENT_STAGE
    directory = Path(sys.argv[1])
    configure(directory)
    report_progress("Przygotowanie DOCX")
    config = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    formats = config["formats"]
    if (not formats and not config.get("include_docx") and not config.get("inspect")) or set(
        formats
    ) - {"pdf", "epub"}:
        return 3
    try:
        if config.get("prepare") or config.get("inspect"):
            if __package__:
                from .document_preparation import prepare_docx, DEFAULT_EDITORIAL_RULES
                from .document_rebuild import inspect_docx
            else:
                from document_preparation import prepare_docx, DEFAULT_EDITORIAL_RULES
                from document_rebuild import inspect_docx
            source = directory / "source.docx"
            if config.get("inspect"):
                with source.open("rb") as document:
                    inspect_docx(document)
                return 0
            rules = config.get("cleaner_rules") if config.get("clean") else ()
            if rules is None:
                rules = list(DEFAULT_EDITORIAL_RULES)
            with (
                source.open("rb") as document,
                prepare_docx(
                    document,
                    rebuild=config.get("rebuild", False),
                    normalize_formatting=config.get("normalize", True),
                    justify=config.get("justify", False),
                    remove_soft_whitespace=config.get("remove_soft_whitespace", False),
                    cleaner_rules=rules,
                    use_cleaner=config.get("clean", False),
                    allow_omissions=config.get("allow_rebuild_omissions", False),
                ) as prepared,
            ):
                payload = prepared.read()
            source.write_bytes(payload)
        if config.get("repetitions") is not None:
            CURRENT_STAGE = "Powtórzenia"
            report_progress("Analiza i kolorowanie powtórzeń")
            if __package__:
                from .document_repetitions import color_document
            else:
                from document_repetitions import color_document
            source = directory / "source.docx"
            with (
                source.open("rb") as document,
                color_document(document, **config["repetitions"]) as marked,
            ):
                payload = marked.read()
            source.write_bytes(payload)
        if formats:
            convert(directory / "source.docx", directory, formats, config["title"])
    except Exception as error:
        # Never record exception messages, locals, source lines or document text.
        versions = {}
        for package in (
            "fpdf2",
            "fpdf",
            "fonttools",
            "mammoth",
            "EbookLib",
            "python-docx",
            "Pillow",
            "spacy",
            "pl_core_news_sm",
        ):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = "not installed"
        frames = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)
        ]
        report = {
            "stage": CURRENT_STAGE,
            "error": type(error).__name__,
            "omissions": getattr(error, "omissions", []),
            "frames": frames,
            "python": sys.version.split()[0],
            "versions": versions,
        }
        try:
            from .document_errors import DocumentInputError, MESSAGES
        except ImportError:
            from document_errors import DocumentInputError, MESSAGES
        if isinstance(error, DocumentInputError) and error.public_code in MESSAGES:
            report["public_code"] = error.public_code
        try:
            (directory / "error.json").write_text(json.dumps(report), encoding="utf-8")
        except OSError:
            pass
        return 2 if isinstance(error, ImportError) else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
