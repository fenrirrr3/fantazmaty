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
    from .document_styles import paragraph_properties
    from .document_html import (PAGE_BREAK_STYLE, list_paragraphs, matched_paragraph, paragraph_queues,
                                scene_break, split_page_breaks)
else:
    from document_progress import configure, report as report_progress
    from document_styles import paragraph_properties
    from document_html import (PAGE_BREAK_STYLE, list_paragraphs, matched_paragraph, paragraph_queues,
                               scene_break, split_page_breaks)

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
.scene-break { text-align: center; text-indent: 0; margin: 1.5em 0; }
hr.page-break { border: 0; height: 0; margin: 3em 0 0; }
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
            if element.tag == "br" and key == "class":
                allowed = value == "page-break"  # znacznik podziału strony z mapy stylów Mammotha
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
    # Styl bez definicji: Mammoth zgłasza go drugi raz jako „None”; wystarczy uwaga niżej.
    (r"Unrecognised (?:paragraph|run|table) style: None \(Style ID: .*\)", lambda m: None),
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
        if text is None:
            continue
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
        while len(group) > 1 and group[-1].tag == "hr" and group[-1].get("class") == "page-break":
            group.pop()  # odstęp po podziale strony nie jest potrzebny na końcu rozdziału
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


def cross_chapter_links(chapters):
    """Odnośniki „#id” do kotwic w innym rozdziale EPUB (np. przypisy) wskazują jego plik.

    chapters: [(nazwa pliku, HTML)]; zwraca listę HTML w tej samej kolejności.
    Rozdział bez takich odnośników zostaje bez zmian.
    """
    from lxml import html
    if len(chapters) < 2:
        return [markup for _, markup in chapters]
    roots = [html.fragment_fromstring(markup or "<p></p>", create_parent="div") for _, markup in chapters]
    owner = {}
    for (name, _), root in zip(chapters, roots):
        for element in root.xpath(".//*[@id]"):
            owner.setdefault(element.get("id"), name)
    result = []
    for (name, markup), root in zip(chapters, roots):
        changed = False
        for link in root.xpath(".//a[starts-with(@href, '#')]"):
            target = owner.get(link.get("href")[1:])
            if target is not None and target != name:
                link.set("href", target + link.get("href"))
                changed = True
        result.append("".join(html.tostring(child, encoding="unicode") for child in root)
                      if changed else markup)
    return result


def epub_markup(root):
    """Treść e-booka: podział strony jako odstęp (hr.page-break), nie nowy plik rozdziału.

    E-book nie ma stron, więc nagłówków i stopek DOCX nie powtarzamy (Mammoth ich nie czyta):
    numer strony czy tytuł w żywej paginie nic czytelnikowi nie mówią, a tytuł jest w metadanych.
    """
    from copy import deepcopy
    from lxml import html

    root = deepcopy(root)
    split_page_breaks(root)
    started = False
    for node in list(root):
        if node.attrib.pop("data-page-break", None) and started:
            node.addprevious(html.Element("hr", {"class": "page-break"}))
        started = started or bool(node.text_content().strip() or node.xpath(".//img"))
    return "".join(html.tostring(child, encoding="unicode") for child in root)


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
        if __package__:
            from .document_errors import DocumentInputError
        else:
            from document_errors import DocumentInputError
        if len(data) > 20 * 1024 * 1024:
            raise DocumentInputError("image_too_large")
        try:
            picture = Image.open(BytesIO(data))
        except Exception:  # noqa: BLE001 – EMF/WMF, SVG, uszkodzony plik: Pillow zgłasza różne błędy
            raise DocumentInputError("unsupported_image") from None
        with picture:
            if picture.width * picture.height > 20_000_000:
                raise DocumentInputError("image_too_large")
            # Rasterize only known image formats, never embed SVG scripts/URLs.
            if picture.format not in ("PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF"):
                raise DocumentInputError("unsupported_image")
            try:
                picture.load()
                rendered = BytesIO()
                picture.convert("RGBA").save(rendered, format="PNG")
            except (OSError, ValueError, ZeroDivisionError, SyntaxError):
                raise DocumentInputError("unsupported_image") from None
        data = rendered.getvalue()
        total_image_bytes += len(data)
        if total_image_bytes > 30 * 1024 * 1024:
            raise DocumentInputError("too_many_images")
        name = f"assets/image-{len(assets) + 1}.png"
        assets[name] = data
        return {"src": name}

    with source.open("rb") as document:
        result = mammoth.convert_to_html(
            document,
            convert_image=mammoth.images.img_element(convert_image),
            external_file_access=False,
            include_embedded_style_map=False,
            style_map="u => u\n" + PAGE_BREAK_STYLE,
            ignore_empty_paragraphs=False,
        )
    if any(message.type == "error" for message in result.messages):
        raise ValueError("Document could not be read completely")
    warnings = polish_warnings(
        str(message.message)[:500] for message in result.messages if message.type == "warning"
    )
    content, headings = sanitize_html(result.value, assets)
    # Mammoth deliberately omits paragraph geometry. Restore alignment from DOCX
    # after sanitization; only our own allowlisted classes reach the EPUB.
    from docx import Document
    from lxml import html

    document = Document(source)
    paragraphs = paragraph_queues(document)
    root = html.fragment_fromstring(content or "<p></p>", create_parent="div")
    list_paragraphs(root)
    for node in root.iterdescendants():
        if node.tag not in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            continue
        paragraph = matched_paragraph(paragraphs, node)
        if paragraph is not None:
            properties = paragraph_properties(paragraph, ("alignment", "page_break_before"))
            node.set(
                "class",
                {0: "align-left", 1: "align-center", 2: "align-right", 3: "align-justify"}.get(
                    properties["alignment"], "align-left"
                ),
            )
            if properties["page_break_before"] and node.getparent() is root:
                node.set("data-page-break", "1")
    for node in root:
        if scene_break(node):
            node.set("class", "scene-break")
    for node in root.iter("p"):
        if not node.text_content() and len(node) == 0:
            node.text = "\u00a0"
    # PDF dopasowuje akapity DOCX ponownie, więc dostaje treść ze znacznikami podziału strony.
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
        parts = split_chapters(epub_markup(root), title)
        names = ["content.xhtml"] if len(parts) == 1 else [
            f"chapter-{number:03d}.xhtml" for number in range(1, len(parts) + 1)]
        linked = cross_chapter_links([(name, markup) for name, (_, markup, _) in zip(names, parts)])
        for name, chapter_content, (chapter_title, _, chapter_headings) in zip(names, linked, parts):
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
        warnings.extend(render_pdf(source, directory / "document.pdf", content, assets, title, document))
    if warnings:
        (directory / "warnings.json").write_text(json.dumps(warnings[:30]), encoding="utf-8")


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
            keep_page_format = bool(config.get("keep_page_format", False))
            if config.get("inspect"):
                with source.open("rb") as document:
                    if keep_page_format:
                        inspect_docx(document, keep_page_format=True)
                    else:
                        inspect_docx(document)
                return 0
            rules = config.get("cleaner_rules") if config.get("clean") else ()
            if rules is None:
                rules = list(DEFAULT_EDITORIAL_RULES)
            # Podsumowanie zmian Odkurzacza: {reguła: liczba zmian}, zapisywane w summary.json.
            summary = {} if config.get("cleaner_summary") else None
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
                    track_changes=bool(config.get("track_changes", False)),
                    replacements=config.get("replacements"),
                    summary=summary,
                    keep_page_format=keep_page_format,
                ) as prepared,
            ):
                payload = prepared.read()
            source.write_bytes(payload)
            if summary is not None:
                (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
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
                color_document(
                    document,
                    **config["repetitions"],
                    # Tylko superuser (ustawiane po stronie serwera): bez limitu wielkości analizy.
                    **({"analysis_unlimited": True} if config.get("analysis_unlimited") else {}),
                ) as marked,
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
