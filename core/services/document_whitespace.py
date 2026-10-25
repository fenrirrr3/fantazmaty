"""Replace soft line breaks with paragraphs and nonbreaking spaces with spaces.

Miękki enter w punkcie listy albo w nagłówku zostaje: podział tworzyłby nowy punkt listy
lub drugi nagłówek (spis treści, EPUB). Kopie elementów rozdzielanych akapitów dostają
nowe w:id, bo identyfikatory zmian i kontrolek muszą być w paczce unikalne.

Śledzone zmiany i komentarze zostają: tekst usunięty (w:del, w:moveFrom) nie jest zmieniany ani
dzielony, znak akapitu źródła (także wstawiony lub usunięty) zostaje przy ostatniej części, a nowy
znak akapitu w miejscu miękkiego entera wstawionego śledzoną zmianą jest wstawiony przez tę samą
zmianę (odrzucenie jej łączy akapity z powrotem). Zakresy komentarzy mogą obejmować kilka akapitów.
"""
from copy import deepcopy
from io import BytesIO
import re
from zipfile import ZipFile
from lxml import etree

if __package__:
    from .document_styles import ParagraphOutline
    from .document_xml import WORD, parse_part, serialize_part
else:
    from document_styles import ParagraphOutline
    from document_xml import WORD, parse_part, serialize_part

SPACE = '{http://www.w3.org/XML/1998/namespace}space'
PART = re.compile(r'word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml\Z')
# Usunięty tekst (śledzone zmiany) nie wyznacza granic akapitów po akceptacji zmian.
DELETED = {WORD+'del', WORD+'moveFrom'}
# Zmiany śledzone treści: wstawki (znak akapitu w nich jest wstawiony) i wszystkie (znak akapitu w:pPr/w:rPr).
INSERTIONS = {WORD+'ins', WORD+'moveTo'}
MARK_REVISIONS = INSERTIONS | DELETED
NONBREAKING = str.maketrans(dict.fromkeys('\u00a0\u202f\u2007', ' '))
WORD_ID = WORD+'id'
# Atrybut w:id="…" (komentarze, zakładki, zmiany) i element <w:id w:val="…"/> (kontrolki zawartości).
ID_RE = re.compile(rb'\bw:id="(\d+)"|<w:id\s+w:val="(\d+)"')


class _Ids:
    """Kolejne wolne w:id (większe od wszystkich w paczce)."""

    def __init__(self, archive):
        highest = 0
        for entry in archive.infolist():
            if entry.filename.endswith('.xml'):
                found = (int(attribute or element) for attribute, element in ID_RE.findall(archive.read(entry)))
                highest = max([highest, *found])
        self.next = highest + 1

    def take(self):
        self.next += 1
        return str(self.next - 1)

    def renumber(self, element):
        for node in element.iter():
            if node.get(WORD_ID) is not None:
                node.set(WORD_ID, self.take())
            elif node.tag == WORD_ID and node.get(WORD+'val') is not None:  # w:sdtPr/w:id
                node.set(WORD+'val', self.take())


def _soft_break(node):
    return node.tag == WORD+'cr' or (
        node.tag == WORD+'br' and node.get(WORD+'type', 'textWrapping') == 'textWrapping')


def _split_inline(node, ids, boundaries, *, paragraph=False):
    """Propagate paragraph boundaries through runs/hyperlinks, retaining formatting.

    boundaries dostaje kolejne miejsca podziału (węzeł miękkiego entera albo w:t z U+2028)."""
    if _soft_break(node):
        boundaries.append(node)
        return [None, None]
    if node.tag == WORD+'t' and '\u2028' in (node.text or ''):
        boundaries.extend([node] * node.text.count('\u2028'))
        parts = []
        for text in node.text.split('\u2028'):
            part = deepcopy(node)
            part.text = text
            part.set(SPACE, 'preserve')
            parts.append(part)
        return parts
    # Text-box paragraphs are processed separately, not as outer run content.
    if node.tag == WORD+'p' and not paragraph or node.tag in DELETED:
        return [deepcopy(node)]
    if not len(node):
        return [deepcopy(node)]
    properties = {WORD+'pPr', WORD+'rPr', WORD+'sdtPr', WORD+'sdtEndPr'}

    def shell(copy=False):
        result = etree.Element(node.tag, attrib=dict(node.attrib), nsmap=node.nsmap)
        result.text = node.text
        for child in node:
            if child.tag in properties:
                result.append(deepcopy(child))
        if copy:
            # Druga i dalsze części: w:ins, w:sdt, zmiany formatowania i znaku akapitu – nowe w:id.
            ids.renumber(result)
        return result

    result = [shell()]
    for child in node:
        if child.tag in properties:
            continue
        parts = _split_inline(child, ids, boundaries)
        if parts[0] is not None:
            result[-1].append(parts[0])
        for part in parts[1:]:
            result.append(shell(copy=True))
            if part is not None:
                result[-1].append(part)
    result[-1].tail = node.tail
    return result


def _drop_empty_revisions(part):
    """Puste opakowania zmian (w:ins bez tekstu – najwyżej z pustymi przebiegami) zostawione przez podział;
    znaczniki znaku akapitu (w:pPr/w:rPr/w:ins) są puste z definicji i zostają."""
    for node in reversed(list(part.iter(*MARK_REVISIONS))):
        if node.getparent().tag != WORD+'rPr' and all(
                child.tag == WORD+'r' and all(item.tag == WORD+'rPr' for item in child) for child in node):
            node.getparent().remove(node)


def _new_paragraph_mark(part, boundary, ids):
    """Znak akapitu w miejscu miękkiego entera: bez zmian znaku źródła (te należą do ostatniej części);
    wstawiony przez tę samą zmianę, gdy miękki enter był we wstawce (w:ins, w:moveTo)."""
    props = part.find(WORD+'pPr')
    mark = props.find(WORD+'rPr') if props is not None else None
    if mark is not None:
        for node in [child for child in mark if child.tag in MARK_REVISIONS]:
            mark.remove(node)
    inserted = next((node for node in boundary.iterancestors(*INSERTIONS, WORD+'p') if node.tag in INSERTIONS), None)
    if inserted is None:
        return
    if props is None:
        props = etree.Element(WORD+'pPr')
        part.insert(0, props)
    if mark is None:
        mark = etree.SubElement(props, WORD+'rPr')
        # Znak akapitu stoi w w:pPr przed podziałem sekcji i zmianą formatowania akapitu.
        for following in (WORD+'sectPr', WORD+'pPrChange'):
            if props.find(following) is not None:
                props.find(following).addprevious(mark)
                break
    revision = etree.Element(inserted.tag, {WORD_ID: ids.take(), **{
        key: value for key, value in inserted.attrib.items() if key in (WORD+'author', WORD+'date')}})
    mark.insert(0, revision)


def _paragraphs_from_soft_breaks(root, ids, outline):
    # Innermost paragraphs first, including text boxes, tables and footnotes.
    for paragraph in reversed(list(root.iter(WORD+'p'))):
        # Nagłówek i punkt listy (document_styles.ParagraphOutline): miękki enter zostaje.
        if outline.heading_level(paragraph) is not None or outline.is_list_item(paragraph):
            continue
        breaks = [node for node in paragraph.iter() if (_soft_break(node)
                  or node.tag == WORD+'t' and '\u2028' in (node.text or ''))
                  and not any(parent.tag in DELETED for parent in node.iterancestors())]
        if not breaks:
            continue
        boundaries = []
        parts = _split_inline(paragraph, ids, boundaries, paragraph=True)
        mark = f'{WORD}pPr/{WORD}rPr/*'
        original = [node.get(WORD_ID) for node in paragraph.iterfind(mark) if node.tag in MARK_REVISIONS]
        for index, part in enumerate(parts):
            if index:
                for key in tuple(part.attrib):
                    if etree.QName(key).localname in ('paraId', 'textId'):
                        del part.attrib[key]
            _drop_empty_revisions(part)
            if index < len(parts)-1:
                _new_paragraph_mark(part, boundaries[index], ids)
            elif index:
                # Znak akapitu źródła (ostatnia część) zachowuje swój w:id; kopia w pierwszej części zniknęła.
                for node, value in zip([node for node in part.iterfind(mark) if node.tag in MARK_REVISIONS], original):
                    node.set(WORD_ID, value)
            props = part.find(WORD+'pPr')
            if props is not None:
                # A section break belongs only to the last resulting paragraph;
                # an explicit page-break-before belongs only to the first.
                for child in list(props):
                    if (child.tag == WORD+'sectPr' and index < len(parts)-1
                            or child.tag == WORD+'pageBreakBefore' and index > 0):
                        props.remove(child)
        parent = paragraph.getparent()
        position = parent.index(paragraph)
        parent.remove(paragraph)
        for index, part in enumerate(parts):
            parent.insert(position+index, part)


def normalize_spacing_docx(source):
    output = BytesIO()
    with ZipFile(source) as archive, ZipFile(output, 'w') as target:
        if len(archive.infolist()) > 2000 or sum(i.file_size for i in archive.infolist()) > 50*1024*1024:
            raise ValueError('Dokument przekracza limit rozpakowanej zawartości.')
        ids = _Ids(archive)
        names = set(archive.namelist())
        outline = ParagraphOutline(parse_part(archive.read('word/styles.xml')) if 'word/styles.xml' in names else None)
        for entry in archive.infolist():
            data = archive.read(entry)
            if PART.fullmatch(entry.filename):
                root = parse_part(data)
                for node in root.iter(WORD+'t'):
                    text = node.text or ''
                    normalized = text.translate(NONBREAKING)
                    # Tekst usunięty śledzoną zmianą (w:moveFrom ma w:t) zostaje taki jak w źródle.
                    if normalized != text and not any(parent.tag in DELETED for parent in node.iterancestors()):
                        node.text = normalized
                        node.set(SPACE, 'preserve')
                _paragraphs_from_soft_breaks(root, ids, outline)
                data = serialize_part(root)
            target.writestr(entry, data)
    output.seek(0)
    return output
