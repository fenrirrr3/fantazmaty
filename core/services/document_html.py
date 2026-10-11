"""Mammoth HTML a akapity DOCX: granice akapitów w listach i dopasowanie akapitów.

Mammoth nie zachowuje geometrii akapitu (wyrównanie, odstępy), więc HTML
łączymy z akapitami DOCX po ich tekście. Tekst po obu stronach liczymy tak
samo jak Mammoth: z poprawkami wstawionymi, bez usuniętych, bez numerów
przypisów i z miękkim enterem jako odstępem.
"""
from collections import defaultdict, deque

WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
CHOICE = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice'
# Treść, której Mammoth nie wstawia do akapitu (usunięte poprawki, kod pola,
# pole proste w:fldSimple, akapity pól tekstowych liczone osobno, właściwości).
_SKIPPED = {WORD + name for name in ('del', 'moveFrom', 'p', 'pPr', 'rPr', 'instrText', 'delText', 'fldSimple')} | {CHOICE}
_SPACES = {WORD + name for name in ('tab', 'ptab', 'br', 'cr')}
_CHARACTERS = {WORD + 'noBreakHyphen': '\u2011', WORD + 'softHyphen': '\u00ad'}
_NOTE_REFERENCES = ('footnote-ref-', 'endnote-ref-')


def list_paragraphs(root):
    from lxml import html

    blocks = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ol', 'ul', 'table', 'blockquote'}
    for item in root.xpath('.//li'):
        paragraph = html.Element('p')
        paragraph.text, item.text = item.text, None
        for child in list(item):
            if child.tag in blocks:
                if paragraph.text or len(paragraph):
                    item.insert(item.index(child), paragraph)
                paragraph = html.Element('p')
                paragraph.text, child.tail = child.tail, None
            else:
                paragraph.append(child)
        if paragraph.text or len(paragraph):
            item.append(paragraph)


def _normalize(value):
    return ' '.join(value.split())


def docx_paragraph_key(element):
    """Tekst akapitu w:p tak, jak trafia do HTML Mammotha (z odstępami ujednoliconymi)."""
    parts = []

    def walk(node):
        for child in node:
            tag = child.tag
            if tag in _SKIPPED:
                continue
            if tag == WORD + 't':
                parts.append(child.text or '')
            elif tag in _SPACES:
                parts.append(' ')
            elif tag in _CHARACTERS:
                parts.append(_CHARACTERS[tag])
            elif len(child):
                walk(child)

    walk(element)
    return _normalize(''.join(parts))


def html_paragraph_key(node):
    """Tekst akapitu HTML bez odnośników przypisów; <br> liczy się jako odstęp."""
    parts = []

    def walk(element):
        if element.text:
            parts.append(element.text)
        for child in element:
            if child.tag == 'br':
                parts.append(' ')
            elif not (child.tag == 'a' and (child.get('id') or '').startswith(_NOTE_REFERENCES)):
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return _normalize(''.join(parts))


def paragraph_queues(document):
    """Akapity DOCX (z tabel i pól tekstowych) w kolejności dokumentu, pogrupowane po tekście."""
    from docx.text.paragraph import Paragraph

    queues = defaultdict(deque)
    for element in document.element.body.iter(WORD + 'p'):
        # Mammoth czyta zapasową wersję pola tekstowego (mc:Fallback), nie mc:Choice.
        if any(ancestor.tag == CHOICE for ancestor in element.iterancestors()):
            continue
        key = docx_paragraph_key(element)
        if key:
            queues[key].append(Paragraph(element, document))
    return queues


def matched_paragraph(queues, node):
    """Następny akapit DOCX o tym samym tekście co węzeł HTML albo None."""
    matches = queues.get(html_paragraph_key(node))
    return matches.popleft() if matches else None


# Mammoth zamienia podział strony Worda (Ctrl+Enter, w:br w:type="page") na ten znacznik. Podział kolumny
# w tekście jednoszpaltowym Word też przenosi na następną stronę (bywa przed tytułem rozdziału).
PAGE_BREAK_STYLE = "br[type='page'] => br.page-break\nbr[type='column'] => br.page-break"
PAGE_BREAK_MARKER = './/br[@class="page-break"]'
# Znaki separatora sceny: akapit złożony wyłącznie z nich (i odstępów), np. „*”, „* * *”, „#”, „⁂”, „~”, „—”.
# Bez „_” i „=”: linie z podkreśleń to zwykle miejsce do wpisania tekstu, nie przerwa w narracji.
SCENE_BREAK_CHARACTERS = frozenset('*∗✱✲✳✻✼❋⁂#~∼•·◆◇◊❖❦❧§⸻—–-')
# Pojedyncza kreska, punktor czy paragraf to zwykle urwana kwestia dialogu, pusty punkt listy albo odsyłacz.
_LONE_NOT_SCENE_BREAK = frozenset('—–-•·§')


def scene_break(node):
    """Akapit będący separatorem sceny: najwyżej 15 znaków, same znaki separatora i odstępy, bez obrazów."""
    text = node.text_content()
    marks = ''.join(text.split())
    return (node.tag == 'p' and 0 < len(marks) and len(text.strip()) <= 15
            and set(marks) <= SCENE_BREAK_CHARACTERS and not node.xpath('.//img')
            and not (len(marks) == 1 and marks in _LONE_NOT_SCENE_BREAK))


def heading_label(node):
    """Etykieta nagłówka w spisie treści: tekst bez numerów przypisów, najwyżej 200 znaków („” gdy pusty)."""
    return html_paragraph_key(node)[:200]


def _has_content(node):
    return bool(node.text_content().strip() or node.xpath('.//img'))


def _split_at(node, marker):
    """Kopie węzła przed znacznikiem i po nim (bez znacznika); powielone id zostają tylko w pierwszej."""
    from copy import deepcopy

    path, element = [], marker
    while element is not node:
        parent = element.getparent()
        path.append(parent.index(element))
        element = parent
    path.reverse()
    before, after = deepcopy(node), deepcopy(node)
    current = before
    for index in path:
        children = list(current)
        for later in children[index + 1:]:
            current.remove(later)
        current = children[index]
        current.tail = None
    current.getparent().remove(current)
    current = after
    for index in path:
        current.attrib.pop('id', None)
        children = list(current)
        current.text = None
        for earlier in children[:index]:
            current.remove(earlier)
        current = children[index]
    parent = current.getparent()
    parent.text = current.tail
    parent.remove(current)
    return before, after


def _carry_anchors(dropped, part):
    """Kotwice (id) z pustej części przed podziałem przenosimy do części z treścią.

    Tak Word zapisuje rozdział od nowej strony: podział na początku akapitu Nagłówka 1. Bez tego
    nagłówek tracił id, a z nim pozycję w spisie treści EPUB i cel odnośników.
    """
    for empty in dropped:
        if empty.get('id') and not part.get('id'):
            part.set('id', empty.get('id'))
        anchors = [element for element in empty.iterdescendants() if element.get('id')]
        for anchor in reversed(anchors):
            if anchor.getparent() is not None:
                anchor.getparent().remove(anchor)
            anchor.tail = part.text
            part.text = None
            part.insert(0, anchor)


def split_page_breaks(root):
    """Rozetnij bloki najwyższego poziomu w miejscach podziału strony i oznacz bloki od nowej strony.

    Blok zaczynający nową stronę dostaje atrybut data-page-break z liczbą podziałów przed nim (kilka
    podziałów z rzędu to puste strony w PDF; EPUB robi z nich jeden odstęp). Akapit z samym podziałem
    (tak zapisuje go Word po Ctrl+Enter) znika, a nowa strona zaczyna się od następnego bloku.
    Podział wewnątrz listy lub tabeli staje się zwykłym końcem wiersza. Wywołuj po dopasowaniu
    akapitów DOCX: rozcięcie zmienia tekst akapitu.
    """
    pending, dropped = 0, []
    for node in list(root):
        markers = node.xpath(PAGE_BREAK_MARKER) if isinstance(node.tag, str) else []
        if node.tag not in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            for marker in markers:
                del marker.attrib['class']
            if pending:
                node.set('data-page-break', str(pending))
            pending, dropped = 0, []
            continue
        parts, rest = [], node
        for marker in markers:
            before, rest = _split_at(rest, rest.xpath(PAGE_BREAK_MARKER)[0])
            parts.append(before)
            parts.append(None)  # podział strony
        parts.append(rest)
        if len(parts) == 1:
            if pending:
                node.set('data-page-break', str(max(pending, int(node.get('data-page-break') or 0))))
            _carry_anchors(dropped, node)
            pending, dropped = 0, []
            continue
        position = root.index(node)
        root.remove(node)
        for part in parts:
            if part is None:
                pending += 1
            elif _has_content(part):
                _carry_anchors(dropped, part)
                if pending:
                    part.set('data-page-break', str(pending))
                pending, dropped = 0, []
                root.insert(position, part)
                position += 1
            else:
                dropped.append(part)
