"""Simple paginated PDF with DOCX page and paragraph settings, bundled fonts.

Strony mają nagłówki i stopki pierwszej sekcji DOCX (z polami numeru strony), a gdy dokument
nie ma stopki – wyśrodkowany numer strony. Podziały stron Worda zaczynają nową stronę,
a Nagłówki 1–3 tworzą konspekt (zakładki) PDF.
render_pdf zwraca uwagi dla użytkownika (np. znaki spoza czcionki).
"""
from base64 import b64encode
from pathlib import Path


if __package__:
    from .document_progress import report
    from .document_styles import paragraph_properties, paragraph_style, style_chain
    from .document_html import (CHOICE, WORD, list_paragraphs, matched_paragraph, paragraph_queues, scene_break,
                                split_page_breaks)
else:
    from document_progress import report
    from document_styles import paragraph_properties, paragraph_style, style_chain
    from document_html import (CHOICE, WORD, list_paragraphs, matched_paragraph, paragraph_queues, scene_break,
                               split_page_breaks)

BLOCKS = ("p", "h1", "h2", "h3", "h4", "h5", "h6")
# Znaczniki, które fpdf2 przyjmuje w komórce tabeli, o ile obejmują cały jej tekst.
CELL_INLINE = {"strong", "em", "u", "s", "del", "sup", "sub", "span", "a"}
CELL_NOTE = ("W PDF komórki tabel z kilkoma akapitami, mieszanym formatowaniem, obrazami obok tekstu "
             "lub tabelą zagnieżdżoną zawierają sam tekst.")
TALL_TABLE_NOTE = ("W PDF tabelę z wierszem dłuższym niż strona złożono jako zwykłe akapity "
                   "(komórka po komórce).")
# Strona bez ustawień w DOCX (brak w:sectPr lub w:pgSz): A4 z marginesami 2,5 cm.
DEFAULT_PAGE = (210.0, 297.0)
DEFAULT_MARGIN = 25.0
# Wcięcie kolejnego poziomu listy (mm).
LIST_INDENT = 6.35
# Znaki spoza czcionki dokumentu zastępowane najbliższym odpowiednikiem: łączniki nierozdzielające
# i typograficzne → „-”, wąskie i cyfrowe spacje nierozdzielające → spacja nierozdzielająca (fpdf2 nie łamie
# na niej wiersza), pozostałe spacje typograficzne → zwykła spacja, znaki zerowej szerokości znikają.
PDF_CHARACTERS = str.maketrans({
    '\u2010': '-', '\u2011': '-', '\u2012': '-',
    '\u202f': '\u00a0', '\u2007': '\u00a0',
    **{chr(code): ' ' for code in (0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2008, 0x2009,
                                   0x200a, 0x205f)},
    '\u200b': None, '\u2060': None, '\ufeff': None,
    '\u223c': '~',
})
SCENE_BREAK_TEXT = '* * *'


def _plain_link_targets(root):
    """fpdf2 nie obsługuje zakładek ani odnośników wewnątrz dokumentu: zostaje sam tekst."""
    for link in root.xpath(".//a"):
        href = link.get("href") or ""
        if href[1:].startswith(("footnote-ref-", "endnote-ref-")):
            link.drop_tree()  # strzałka powrotu z przypisu nie ma dokąd prowadzić
        elif not href or href.startswith("#"):
            link.drop_tag()


def _single_inline(element):
    """Akapit z samym tekstem albo jednym łańcuchem znaczników obejmującym cały tekst."""
    while True:
        children = list(element)
        if not children:
            return True
        child = children[0]
        if len(children) > 1 or (element.text or "").strip() or (child.tail or "").strip():
            return False
        if child.tag == "img":
            return True
        if child.tag not in CELL_INLINE:
            return False
        element = child


def _cell_lines(cell):
    lines = []
    for block in cell.iter(*BLOCKS):
        for br in block.iter("br"):
            br.tail = "\n" + (br.tail or "")
        text = "\n".join(" ".join(line.split()) for line in block.text_content().split("\n"))
        item = block.getparent()
        if item is not None and item.tag == "li" and item.index(block) == 0:
            parent = item.getparent()
            marker = "•" if parent is None or parent.tag != "ol" else f"{parent.index(item) + 1}."
            text = marker + " " + text
        if text.strip():
            lines.append(text.strip("\n"))
    if not lines and cell.text_content().strip():
        lines.append(" ".join(cell.text_content().split()))
    return lines


def _simplify_cells(root):
    """fpdf2 mieści w komórce tylko jeden fragment tekstu: resztę spłaszczamy. Zwraca True przy uproszczeniu."""
    from lxml import html
    simplified = False
    for cell in root.xpath(".//td[not(ancestor::td or ancestor::th)] | .//th[not(ancestor::td or ancestor::th)]"):
        children = list(cell)
        if not children or (len(children) == 1 and not (cell.text or "").strip()
                            and not (children[0].tail or "").strip()
                            and (children[0].tag == "img"
                                 or children[0].tag in BLOCKS and _single_inline(children[0]))):
            continue
        lines = _cell_lines(cell)
        images = cell.xpath(".//img")
        align = next((block.get("align") for block in cell.iter(*BLOCKS) if block.get("align")), None)
        lost = bool(cell.xpath(".//strong | .//em | .//u | .//s | .//del | .//sup | .//sub | .//table")
                    or (images and lines) or len(images) > 1)
        for child in list(cell):
            cell.remove(child)
        cell.text = None
        if lines:
            paragraph = html.Element("p")
            if align:
                paragraph.set("align", align)
            paragraph.text = "\n".join(lines)
            cell.append(paragraph)
        elif images:
            images[0].tail = None
            cell.append(images[0])
        simplified = simplified or lost
    return simplified


def _font_size(paragraph):
    """Rozmiar czcionki akapitu w pt (pierwszy przebieg z tekstem, potem styl) albo None."""
    font_size = next(
        (r.font.size for r in paragraph.runs if r.text.strip() and r.font.size is not None),
        None,
    )
    if font_size is None:
        for style in style_chain(paragraph_style(paragraph)):
            font_size = style.font.size
            if font_size is not None:
                break
    return None if font_size is None else font_size.pt


# Pola Worda w nagłówku i stopce zastępowane przy składzie każdej strony.
PAGE_FIELD, PAGES_FIELD = '', ''
FIELDS = {'PAGE': PAGE_FIELD, 'NUMPAGES': PAGES_FIELD, 'SECTIONPAGES': PAGES_FIELD}
_HIDDEN = {WORD + name for name in ('del', 'moveFrom', 'p', 'pPr', 'rPr', 'delText')} | {CHOICE}


def _field(instruction):
    words = (instruction or '').split()
    return FIELDS.get(words[0].upper()) if words else None


def _story_text(paragraph):
    """Tekst akapitu nagłówka lub stopki; pola numeru strony jako PAGE_FIELD/PAGES_FIELD, inne pola – ich wynik."""
    parts, fields = [], []  # fields: [kod pola, czy w części wyniku, czy wynik zastąpiony]

    def walk(node):
        for child in node:
            tag = child.tag
            if tag in _HIDDEN:
                continue
            if tag == WORD + 'fldChar':
                kind = child.get(WORD + 'fldCharType')
                if kind == 'begin':
                    fields.append(['', False, False])
                elif fields and kind in ('separate', 'end') and not fields[-1][1]:
                    fields[-1][1] = True
                    token = _field(fields[-1][0])
                    if token:
                        parts.append(token)
                        fields[-1][2] = True
                if fields and kind == 'end':
                    fields.pop()
            elif tag == WORD + 'instrText':
                if fields and not fields[-1][1]:
                    fields[-1][0] += child.text or ''
            elif len(child) and tag != WORD + 'fldSimple':
                walk(child)
            elif any(not result or replaced for _, result, replaced in fields):
                continue  # kod pola albo wynik pola, które sami liczymy
            elif tag == WORD + 'fldSimple' and _field(child.get(WORD + 'instr')):
                parts.append(_field(child.get(WORD + 'instr')))
            elif tag == WORD + 't':
                parts.append(child.text or '')
            elif tag in (WORD + 'tab', WORD + 'ptab'):
                parts.append('\t')
            elif tag in (WORD + 'br', WORD + 'cr'):
                parts.append(' ')
            elif tag == WORD + 'noBreakHyphen':
                parts.append('‑')
            elif len(child):
                walk(child)  # wynik innego pola prostego (np. data)

    walk(paragraph._p)
    return ''.join(parts)


def _story_blocks(element):
    """Akapity i tabele nagłówka lub stopki w kolejności (także z kontrolek zawartości i pól tekstowych)."""
    for child in element:
        if child.tag == WORD + 'p':
            yield child
            for nested in child.iter(WORD + 'p'):  # akapity pól tekstowych w tym akapicie
                if nested is not child and not any(ancestor.tag == CHOICE for ancestor in nested.iterancestors()):
                    yield nested
        elif child.tag == WORD + 'tbl':
            yield child
        elif isinstance(child.tag, str) and child.tag != CHOICE:
            yield from _story_blocks(child)


def _story_paragraph(paragraph):
    align = paragraph_properties(paragraph, ('alignment',))['alignment']
    size = _font_size(paragraph)
    return (_story_text(paragraph).translate(PDF_CHARACTERS), {1: 'C', 2: 'R'}.get(align, 'L'),
            12 if size is None else max(6, min(size, 36)))


def _story_lines(story):
    """[(tekst, wyrównanie fpdf, rozmiar pt)] nagłówka lub stopki; [] gdy brak definicji albo sama pustka.

    Wiersz tabeli (częsty układ „autor | tytuł | strona”) to jeden wiersz z częściami do lewej, na środek
    i do prawej, jak tekst z tabulatorami stylu Nagłówek.
    """
    from docx.text.paragraph import Paragraph

    if story.is_linked_to_previous:  # w pierwszej sekcji: brak nagłówka tego rodzaju
        return []
    lines = []
    for block in _story_blocks(story._element):
        if block.tag == WORD + 'p':
            lines.append(_story_paragraph(Paragraph(block, story)))
            continue
        for row in block.iterchildren(WORD + 'tr'):
            cells = []
            for cell in row.iterchildren(WORD + 'tc'):
                paragraphs = [_story_paragraph(Paragraph(element, story)) for element in cell.iterchildren(WORD + 'p')]
                cells.append((' '.join(' '.join(text.split()) for text, _, _ in paragraphs).strip(),
                              max((size for _, _, size in paragraphs), default=12)))
            texts = [text for text, _ in cells]
            if len(texts) == 2:
                texts.insert(1, '')  # dwie komórki: do lewej i do prawej
            if any(texts):
                lines.append(('\t'.join(texts) if len(texts) <= 3 else ' '.join(texts), 'L',
                              max(size for _, size in cells)))
    return lines if any(text.strip() for text, _, _ in lines) else []


def page_stories(document, section):
    """Nagłówki i stopki pierwszej sekcji: {'default'|'first'|'even': (wiersze nagłówka, wiersze stopki)}.

    Wariant pierwszej strony obowiązuje, gdy sekcja ma „Inne na pierwszej stronie”, a parzystych –
    gdy dokument ma „Inne na stronach parzystych i nieparzystych”; brak definicji wariantu = pusty.
    Dokument bez w:sectPr (section None) nie ma nagłówków ani stopek.
    """
    if section is None:
        return {'default': ([], [])}
    stories = {'default': (_story_lines(section.header), _story_lines(section.footer))}
    if section.different_first_page_header_footer:
        stories['first'] = (_story_lines(section.first_page_header), _story_lines(section.first_page_footer))
    if document.settings.odd_and_even_pages_header_footer:
        stories['even'] = (_story_lines(section.even_page_header), _story_lines(section.even_page_footer))
    return stories


def _line_height(pdf, size):
    return size * 1.15 / pdf.k


def _wrap(pdf, text):
    """Wiersze tekstu mieszczące się w szerokości kolumny (łamanie na spacjach, jak multi_cell)."""
    lines, current = [], ''
    for word in text.split(' '):
        candidate = f'{current} {word}' if current else word
        if current and pdf.get_string_width(candidate) > pdf.epw:
            lines.append(current)
            current = word
        else:
            current = candidate
    return [*lines, current]


def _placed(pdf, text, align):
    """[[(część, wyrównanie)] na każdy wiersz]: tabulatory stylu Nagłówek/Stopka to środek i prawy margines,
    a zbyt długi tekst łamie się na kilka wierszy zamiast wychodzić poza stronę."""
    parts = [' '.join(part.split()) for part in text.split('\t')]
    if 1 < len(parts) <= 3 and pdf.get_string_width('   '.join(parts)) <= pdf.epw:
        return [list(zip(parts, ('L', 'C', 'R')))]
    return [[(line, align)] for line in _wrap(pdf, ' '.join(' '.join(parts).split()))]


def _lines_height(pdf, lines):
    """Wysokość nagłówka lub stopki z łamaniem wierszy (pola numeru strony liczone jako „999”)."""
    family, style, size_pt = pdf.font_family, pdf.font_style, pdf.font_size_pt
    total = 0
    for text, align, size in lines:
        pdf.set_font('Document', size=size)
        rows = _placed(pdf, text.replace(PAGE_FIELD, '999').replace(PAGES_FIELD, '999'), align)
        total += len(rows) * _line_height(pdf, size)
    if family:
        pdf.set_font(family, style, size_pt)
    return total


def _draw_lines(pdf, lines, y):
    for text, align, size in lines:
        height = _line_height(pdf, size)
        text = text.replace(PAGE_FIELD, str(pdf.page_no())).replace(PAGES_FIELD, pdf.str_alias_nb_pages or '')
        pdf.set_font('Document', size=size)
        for row in _placed(pdf, text, align):
            for part, place in row:
                if part:
                    pdf.set_xy(pdf.l_margin, y)
                    pdf.cell(w=pdf.epw, h=height, text=part, align=place)
            y += height


def _page_geometry(section):
    """(szerokość, wysokość, [lewy, górny, prawy, dolny margines]) w mm; brakujące wartości – A4 i 2,5 cm.

    Ujemny margines górny lub dolny Word traktuje jako „dokładnie” (tekst może wejść na nagłówek); w PDF
    liczy się jego wartość bezwzględna.
    """
    def value(name, default):
        length = getattr(section, name) if section is not None else None
        return default if length is None else abs(length.mm)
    width, height = value('page_width', DEFAULT_PAGE[0]), value('page_height', DEFAULT_PAGE[1])
    margins = [value(name, DEFAULT_MARGIN) for name in ('left_margin', 'top_margin', 'right_margin', 'bottom_margin')]
    return width, height, margins


def _list_marker(item):
    if item.getparent().tag == 'ol':
        return f'{sum(1 for _ in item.itersiblings("li", preceding=True)) + 1}.'
    return '•'


def _flatten_lists(root):
    """Listy (także przypisy na końcu) jako zwykłe akapity z numerem lub punktorem i wcięciem poziomu.

    fpdf2 gubi numer i punktor, gdy punkt zawiera akapit (<li><p>), a tak wygląda każdy punkt po
    list_paragraphs. Wywołuj po nadaniu akapitom geometrii DOCX.
    """
    for node in list(root):
        if node.tag not in ('ol', 'ul'):
            continue
        blocks = []

        def walk(container, level):
            for item in container.iterchildren('li'):
                marker = _list_marker(item)
                for child in item:
                    if child.tag in ('ol', 'ul'):
                        walk(child, level + 1)
                        continue
                    if child.tag in BLOCKS:
                        if marker:
                            child.text = f'{marker} {child.text or ""}'
                            marker = None
                        child.set('data-left', str(round(level * LIST_INDENT, 2)))
                        child.set('data-indent', '0')
                    blocks.append(child)
        walk(node, 0)
        position = root.index(node)
        root.remove(node)
        for offset, block in enumerate(blocks):
            block.tail = None
            root.insert(position + offset, block)


def _table_paragraphs(table):
    """Tabela jako akapity: każdy niepusty wiersz komórki osobno, z rozmiarem pierwszego akapitu komórki."""
    from lxml import html
    paragraphs = []
    for cell in table.xpath('.//td[not(ancestor::td or ancestor::th)] | .//th[not(ancestor::td or ancestor::th)]'):
        sizes = [block.get('data-size') for block in cell.iter(*BLOCKS) if block.get('data-size')]
        for line in _cell_lines(cell):
            paragraph = html.Element('p', {'align': 'left', 'data-indent': '0', 'data-before': '0',
                                           'data-after': '0', 'line-height': '1.15',
                                           'data-size': sizes[0] if sizes else '12'})
            paragraph.text = ' '.join(line.split())
            paragraphs.append(paragraph)
    return paragraphs


def _pdf_texts(root):
    for element in root.iter():
        if element.text:
            element.text = element.text.translate(PDF_CHARACTERS)
        if element.tail:
            element.tail = element.tail.translate(PDF_CHARACTERS)


def render_pdf(source, target, content, assets, title, document=None):
    from docx import Document
    from fpdf import FPDF
    from fpdf.fonts import TextStyle
    from fpdf.html import HTML2FPDF
    from lxml import html
    if document is None:
        document = Document(source)
    sections = document.sections
    section = sections[0] if len(sections) else None
    width, height, margins = _page_geometry(section)
    if not (50 <= width <= 600 and 50 <= height <= 600):
        raise ValueError('Unsupported page dimensions')
    if margins[0] + margins[2] >= width - 20 or margins[1] + margins[3] >= height - 20:
        raise ValueError('Unsupported page margins')
    class WordHTML(HTML2FPDF):
        def handle_starttag(self, tag, attrs):
            super().handle_starttag(tag, attrs)
            values = dict(attrs)
            if tag == 'p' and self.td_th is not None and values.get('align'):
                self.td_th['align'] = values['align']
            if tag in ('p','h1','h2','h3','h4','h5','h6') and self._paragraph is not None:
                self._paragraph.first_line_indent = float(values.get('data-indent', 0))
                self._paragraph.top_margin = float(values.get('data-before', 0))
                self._paragraph.line_height = float(values.get('line-height', 1.5))
            self.heading_level = None  # konspekt PDF tworzymy sami: cały tytuł, tylko Nagłówki 1–3

    stories = page_stories(document, section)
    automatic_number = not any(footer for _, footer in stories.values()) and not any(
        PAGE_FIELD in text for header, _ in stories.values() for text, _, _ in header)

    class WordPDF(FPDF):
        def story(self, index):
            page = self.page_no()
            variant = ('first' if page == 1 and 'first' in stories
                       else 'even' if page % 2 == 0 and 'even' in stories else 'default')
            return stories[variant][index]

        def draw(self, lines, y):
            family, style, size = self.font_family, self.font_style, self.font_size_pt
            x, current = self.x, self.y
            # Nagłówek lub stopka wyższe niż strona nie mogą wywołać łamania strony (a z nim kolejnego
            # rysowania nagłówka w nieskończoność) – nadmiar wychodzi poza kartkę, konwersja trwa dalej.
            auto, margin = self.auto_page_break, self.b_margin
            self.set_auto_page_break(False)
            try:
                _draw_lines(self, lines, y)
            finally:
                self.set_auto_page_break(auto, margin)
            if family:
                self.set_font(family, style, size)
            self.set_xy(x, current)

        def header(self):
            lines = self.story(0)
            if lines:
                self.draw(lines, header_distance)

        def footer(self):
            lines = self.story(1) or (automatic_number and [(PAGE_FIELD, 'C', 11)])
            if lines:
                self.draw(lines, self.h - footer_distance - _lines_height(self, lines))

    font_dir = Path(__file__).with_name('fonts')
    page_format = (width, height)

    def new_pdf(cls):
        pdf = cls(format=page_format)
        pdf.HTML2FPDF_CLASS = WordHTML
        for style, suffix in (('', 'Regular'), ('B', 'Bold'), ('I', 'Italic'), ('BI', 'BoldItalic')):
            pdf.add_font('Document', style=style, fname=font_dir / ('NimbusRoman-' + suffix + '.ttf'))
        return pdf

    pdf = new_pdf(WordPDF)
    pdf.set_margins(*margins[:3])  # szerokość kolumny do łamania długich nagłówków i stopek
    # Word odsuwa tekst od nagłówka i stopki, które nie mieszczą się w marginesie.
    distances = [None if section is None else section.header_distance,
                 None if section is None else section.footer_distance]
    header_distance, footer_distance = (max(0, min(abs(value.mm) if value is not None else 12.7, height / 4))
                                        for value in distances)
    tallest = [max((_lines_height(pdf, lines[index]) for lines in stories.values()), default=0)
               for index in (0, 1)]
    if automatic_number:
        tallest[1] = max(tallest[1], _line_height(pdf, 11))
    top = max(margins[1], header_distance + tallest[0] if tallest[0] else 0)
    bottom = max(margins[3], footer_distance + tallest[1] if tallest[1] else 0)
    if top + bottom < height - 20:  # bardzo wysoki nagłówek lub stopka zachodzi na tekst, jak w Wordzie
        margins[1], margins[3] = top, bottom
    if any(PAGES_FIELD in text for pair in stories.values() for lines in pair for text, _, _ in lines):
        # fpdf2 wstawia liczbę stron w miejsce aliasu w całym tekście, więc alias nie może wystąpić w treści.
        alias, number = '{nb}', 0
        while alias in (content or ''):
            number += 1
            alias = '{nb%d}' % number
        pdf.alias_nb_pages(alias)
    else:
        pdf.alias_nb_pages(None)
    pdf.set_title(title)
    pdf.set_margins(*margins[:3])
    pdf.set_auto_page_break(True, margins[3])
    pdf.add_page()
    pdf.set_font("Document", size=12)
    supported = pdf.fonts['document'].cmap
    def normalize(value):
        return " ".join(value.split())
    paragraphs = paragraph_queues(document)
    root = html.fragment_fromstring(content or "<p></p>", create_parent="div")
    list_paragraphs(root)
    for img in root.xpath(".//img"):
        key = img.get("src")
        if key not in assets:
            raise ValueError("Unknown image")
        # Embedded image data only: no network or local file paths reach fpdf2.
        img.set("src", "data:image/png;base64," + b64encode(assets[key]).decode("ascii"))
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(assets[key])) as image:
            img.set(
                "width", str(min(image.width * 0.75, (width - margins[0] - margins[2]) * pdf.k))
            )
    # Resolve every paragraph, including those nested in lists and tables.
    for node in root.iterdescendants():
        if node.tag not in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            continue
        paragraph = matched_paragraph(paragraphs, node)
        size, line_height, before, after, indent = 12, 1.5, 0, 0, 12.5
        if paragraph is not None:
            font_size = _font_size(paragraph)
            if font_size is not None:
                size = max(6, min(font_size, 72))
            properties = paragraph_properties(
                paragraph,
                (
                    "line_spacing",
                    "space_before",
                    "space_after",
                    "first_line_indent",
                    "alignment",
                    "page_break_before",
                ),
            )
            spacing = properties["line_spacing"]
            if spacing is not None:
                line_height = spacing.pt / size if hasattr(spacing, "pt") else float(spacing)
                line_height = max(0.8, min(line_height, 4))
            for attr in ("space_before", "space_after"):
                value = properties[attr]
                if value is not None:
                    if attr == "space_before":
                        before = value.mm
                    else:
                        after = value.mm
            value = properties["first_line_indent"]
            if value is not None:
                indent = max(0, min(value.mm, 40))
            align = properties["alignment"]
            if node.tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
                node.set(
                    "align", {0: "left", 1: "center", 2: "right", 3: "justify"}.get(align, "left")
                )
            if properties["page_break_before"]:
                node.set("data-page-break", "1")
        if node.getparent() is root and scene_break(node):
            # Separator sceny („* * *”, „#”, „⁂”…): wyśrodkowany, z odstępem jak w e-booku.
            node.set("align", "center")
            indent, before, after = 0, max(before, 6.35), max(after, 6.35)
            if any(ord(character) not in supported for character in node.text_content() if not character.isspace()):
                for child in list(node):
                    node.remove(child)
                node.text = SCENE_BREAK_TEXT  # ozdobnik spoza czcionki (❦, ⁂, ✻…)
        if node.tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            node.set("line-height", str(line_height))
            node.set("data-indent", str(indent))
            node.set("data-before", str(before))
        node.set("data-size", str(size))
        node.set("data-after", str(after))
    _pdf_texts(root)
    _flatten_lists(root)
    split_page_breaks(root)
    notes = []
    outline_level = -1
    _plain_link_targets(root)
    if _simplify_cells(root):
        notes.append(CELL_NOTE)
    scratch = []

    def fits(table):
        """Czy każdy wiersz tabeli mieści się na stronie: próbny skład na czystej stronie osobnego PDF."""
        if not scratch:
            scratch.append(new_pdf(FPDF))
            scratch[0].set_margins(*margins[:3])
            scratch[0].set_auto_page_break(True, margins[3])
        trial = scratch[0]
        if trial.page >= 50:  # próbne strony tylko zajmują pamięć
            scratch[0] = new_pdf(FPDF)
            scratch[0].set_margins(*margins[:3])
            scratch[0].set_auto_page_break(True, margins[3])
            trial = scratch[0]
        trial.add_page()
        trial.set_font("Document", size=12)
        try:
            trial.write_html(html.tostring(table, encoding="unicode"), tag_styles=styles(12, 0, 0, 0))
        except ValueError:
            return False
        return True

    def styles(size, before, after, left):
        return {
            tag: TextStyle(
                font_family="Document",
                font_size_pt=size,
                font_style="B" if tag.startswith("h") else "",
                color=0,
                t_margin=before,
                b_margin=after,
                l_margin=left,
            )
            for tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code")
        }

    blocks = list(root)
    index = 0
    while index < len(blocks):
        node = blocks[index]
        if index % 10 == 0:
            report("Skład PDF – bloki treści", index, len(blocks))
        index += 1
        if node.tag == "table" and not fits(node):
            # fpdf2 nie dzieli wiersza tabeli między strony: za wysoki wiersz przerwałby cały PDF.
            replacement = _table_paragraphs(node)
            if node.get("data-page-break") and replacement:
                replacement[0].set("data-page-break", node.get("data-page-break"))
            blocks[index:index] = replacement
            if TALL_TABLE_NOTE not in notes:
                notes.append(TALL_TABLE_NOTE)
            continue
        text = normalize(node.text_content())
        breaks = int(node.get("data-page-break") or 0)
        if breaks:
            # Kilka podziałów z rzędu: każdy następny to pusta strona, jak w Wordzie.
            if pdf.y > pdf.t_margin + 1:
                pdf.add_page()
            for _ in range(breaks - 1):
                pdf.add_page()
        if node.tag == "p" and not text and not node.xpath(".//img"):
            empty_line = 18 / pdf.k
            if pdf.will_page_break(empty_line):
                pdf.add_page()
            pdf.ln(empty_line)
            continue
        size = float(node.get("data-size", 12))
        before = float(node.get("data-before", 0))
        after = float(node.get("data-after", 0))
        if node.tag in ("h1", "h2", "h3") and text:
            # Zakładka wskazuje początek nagłówka, więc nagłówek, który i tak trafi na następną stronę,
            # zaczyna ją już teraz. Poziom nie przeskakuje o więcej niż jeden (Nagłówek 3 po 1).
            first_line = before + size * float(node.get("line-height", 1.5)) / pdf.k
            if pdf.will_page_break(first_line) and pdf.y > pdf.t_margin + 1:
                pdf.add_page()
            outline_level = min(int(node.tag[1]) - 1, outline_level + 1)
            pdf.start_section(text[:200], outline_level, strict=False)
        pdf.set_font("Document", size=size)
        pdf.write_html(html.tostring(node, encoding="unicode"),
                       tag_styles=styles(size, before, after, float(node.get("data-left", 0))))
    report("Zapis PDF")
    pdf.output(target)
    missing = sorted({code for font in pdf.fonts.values() for code in getattr(font, "missing_glyphs", ())})
    if missing:
        shown = ", ".join(chr(code) for code in missing[:10]) + (" …" if len(missing) > 10 else "")
        notes.append(f"W PDF pominięto znaki, których nie ma w czcionce dokumentu ({shown}).")
    return notes
