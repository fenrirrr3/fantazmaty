"""Mammoth HTML a akapity DOCX: granice akapitów w listach i dopasowanie akapitów.

Mammoth nie zachowuje geometrii akapitu (wyrównanie, odstępy), więc HTML
łączymy z akapitami DOCX po ich tekście. Tekst po obu stronach liczymy tak
samo jak Mammoth: z poprawkami wstawionymi, bez usuniętych, bez numerów
przypisów i z miękkim enterem jako odstępem.
"""
from collections import defaultdict, deque
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

if __package__:
    from .document_styles import merge_drop_caps
else:
    from document_styles import merge_drop_caps

WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
CHOICE = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice'
# Treść, której Mammoth nie wstawia do akapitu (usunięte poprawki, kod pola,
# pole proste w:fldSimple, akapity pól tekstowych liczone osobno, właściwości).
_SKIPPED = {WORD + name for name in ('del', 'moveFrom', 'p', 'pPr', 'rPr', 'instrText', 'delText', 'fldSimple')} | {CHOICE}
_SPACES = {WORD + name for name in ('tab', 'ptab', 'br', 'cr')}
_CHARACTERS = {WORD + 'noBreakHyphen': '\u2011', WORD + 'softHyphen': '\u00ad'}
_NOTE_REFERENCES = ('footnote-ref-', 'endnote-ref-')


# Kopia DOCX dla Mammotha (mammoth_source): części z treścią, którą czyta Mammoth.
MAMMOTH_PARTS = ('word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml')
_MAMMOTH_UNWRAPPED = {WORD + name for name in ('fldSimple', 'dir', 'bdo')}
_MAMMOTH_DROPPED = {WORD + name for name in ('fldData', 'smartTagPr', 'customXmlPr')}
_MAMMOTH_RENAMED = {WORD + 'cr': WORD + 'br', WORD + 'ptab': WORD + 'tab'}
_GRAPHIC_DATA = '{http://schemas.openxmlformats.org/drawingml/2006/main}graphicData'
_FALLBACK = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback'
# Rysunki bez obrazu i tekstu dla Mammotha: wykres, SmartArt, nowy wykres Office 2016.
_GRAPHIC_KINDS = ('/chart', '/diagram', '/chartex')
MAMMOTH_NOTES = {
    'missing_note': 'Pominięto odsyłacz do przypisu, którego nie ma w dokumencie.',
    'hidden': 'Pominięto tekst ukryty w Wordzie (efekt czcionki „Ukryty”) – jak przy wydruku.',
    'graphic': 'Pominięto wykres lub diagram SmartArt – e-book i PDF go nie zawierają.',
}


def _hidden(properties, hidden_styles):
    """Czy przebieg jest ukryty: własne w:vanish albo styl znakowy z w:vanish (bez dziedziczenia stylów)."""
    if properties is None:
        return False
    vanish = properties.find(WORD + 'vanish')
    if vanish is not None:
        return vanish.get(WORD + 'val', 'true') not in ('0', 'false', 'off')
    style = properties.find(WORD + 'rStyle')
    return style is not None and style.get(WORD + 'val') in hidden_styles


def mammoth_source(data):
    """Kopia DOCX dla Mammotha i dopasowania akapitów PDF: (bajty, uwagi dla użytkownika).

    Mammoth pomija w:fldSimple, w:dir i w:bdo razem z treścią (numer podpisu, pole korespondencji, tekst
    od prawej do lewej), skleja słowa wokół w:cr i w:ptab, pokazuje tekst ukryty i przerywa pracę na
    odsyłaczu do nieistniejącego przypisu. W kopii treść tych elementów to zwykłe przebiegi, w:cr to w:br,
    w:ptab to w:tab, ukrytych przebiegów (poza znacznikami pól) i odsyłaczy donikąd nie ma, a inicjał
    wraca do swojego akapitu. DOCX wynikowy zostaje bez zmian. Uwagi mówią o tym, czego nie będzie
    w e-booku i PDF (także o wykresach i diagramach, które Mammoth pomija bez ostrzeżenia).
    """
    from collections import Counter
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        roots = {name: etree.fromstring(archive.read(name), parser) for name in MAMMOTH_PARTS if name in names}
        styles = etree.fromstring(archive.read('word/styles.xml'), parser) if 'word/styles.xml' in names else None
    hidden_styles = set() if styles is None else {
        style.get(WORD + 'styleId') for style in styles.iter(WORD + 'style')
        if style.get(WORD + 'type') == 'character' and _hidden(style.find(WORD + 'rPr'), ())}
    notes = {}
    for kind in ('footnote', 'endnote'):
        root = roots.get(f'word/{kind}s.xml')
        notes[kind] = set() if root is None else {
            note.get(WORD + 'id') for note in root.iter(WORD + kind) if note.get(WORD + 'type', 'normal') == 'normal'}
    counts, changed = Counter(), set()
    for name, root in roots.items():
        edits = merge_drop_caps(root)
        for run in list(root.iter(WORD + 'r')):
            if (run.getparent() is not None and _hidden(run.find(WORD + 'rPr'), hidden_styles)
                    and run.find(WORD + 'fldChar') is None):
                run.getparent().remove(run)
                counts['hidden'] += 1
                edits += 1
        for kind, valid in notes.items():
            for reference in list(root.iter(WORD + kind + 'Reference')):
                if reference.get(WORD + 'id') not in valid:
                    reference.getparent().remove(reference)
                    counts['missing_note'] += 1
                    edits += 1
        for node in list(root.iter(*_MAMMOTH_DROPPED)):
            node.getparent().remove(node)
            edits += 1
        for node in reversed(list(root.iter(*_MAMMOTH_UNWRAPPED))):
            parent, index = node.getparent(), node.getparent().index(node)
            for child in list(node):
                parent.insert(index, child)
                index += 1
            parent.remove(node)
            edits += 1
        for node in list(root.iter(*_MAMMOTH_RENAMED)):
            node.tag = _MAMMOTH_RENAMED[node.tag]
            node.attrib.clear()
            edits += 1
        counts['graphic'] += sum(1 for node in root.iter(_GRAPHIC_DATA)
                                 if node.get('uri', '').endswith(_GRAPHIC_KINDS)
                                 and not any(ancestor.tag == _FALLBACK for ancestor in node.iterancestors()))
        if edits:
            changed.add(name)
    warnings = [text if counts[key] == 1 else f'{text} ({counts[key]} razy)'
                for key, text in MAMMOTH_NOTES.items() if counts[key]]
    if not changed:
        return data, warnings
    output = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename in changed:
                payload = etree.tostring(roots[item.filename], xml_declaration=True, encoding='UTF-8',
                                         standalone=True)
            target.writestr(item, payload, compress_type=ZIP_DEFLATED)
    return output.getvalue(), warnings

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
