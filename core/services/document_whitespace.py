"""Replace soft line breaks with paragraphs and nonbreaking spaces with spaces.

Miękki enter w punkcie listy albo w nagłówku zostaje: podział tworzyłby nowy punkt listy
lub drugi nagłówek (spis treści, EPUB). Kopie elementów rozdzielanych akapitów dostają
nowe w:id, bo identyfikatory zmian i kontrolek muszą być w paczce unikalne.
"""
from copy import deepcopy
from io import BytesIO
import re
from zipfile import ZipFile
from lxml import etree

WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SPACE = '{http://www.w3.org/XML/1998/namespace}space'
PART = re.compile(r'word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml\Z')
# Usunięty tekst (śledzone zmiany) nie wyznacza granic akapitów po akceptacji zmian.
DELETED = {WORD+'del', WORD+'moveFrom'}
NONBREAKING = str.maketrans(dict.fromkeys('\u00a0\u202f\u2007', ' '))
WORD_ID = WORD+'id'
ID_RE = re.compile(rb'\bw:id="(\d+)"')
HEADING_NAME = re.compile(r'(?:heading|nagłówek|naglowek)\s*\d', re.IGNORECASE)


class _Ids:
    """Kolejne wolne w:id (większe od wszystkich w paczce)."""

    def __init__(self, archive):
        highest = 0
        for entry in archive.infolist():
            if entry.filename.endswith('.xml'):
                highest = max([highest, *map(int, ID_RE.findall(archive.read(entry)))])
        self.next = highest + 1

    def renumber(self, element):
        for node in element.iter():
            if node.get(WORD_ID) is not None:
                node.set(WORD_ID, str(self.next))
                self.next += 1
            elif node.tag == WORD_ID and node.get(WORD+'val') is not None:  # w:sdtPr/w:id
                node.set(WORD+'val', str(self.next))
                self.next += 1


def _soft_break(node):
    return node.tag == WORD+'cr' or (
        node.tag == WORD+'br' and node.get(WORD+'type', 'textWrapping') == 'textWrapping')


def _split_inline(node, ids, *, paragraph=False):
    """Propagate paragraph boundaries through runs/hyperlinks, retaining formatting."""
    if _soft_break(node):
        return [None, None]
    if node.tag == WORD+'t' and '\u2028' in (node.text or ''):
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
        parts = _split_inline(child, ids)
        if parts[0] is not None:
            result[-1].append(parts[0])
        for part in parts[1:]:
            result.append(shell(copy=True))
            if part is not None:
                result[-1].append(part)
    result[-1].tail = node.tail
    return result


def _keeps_lines(paragraph, styles):
    """Punkt listy albo nagłówek (bezpośrednio lub ze stylu): miękki enter zostaje."""
    props = paragraph.find(WORD+'pPr')
    if props is None:
        style = None
    else:
        numbering = props.find(WORD+'numPr')
        if numbering is not None:
            number = numbering.find(WORD+'numId')
            return number is None or number.get(WORD+'val') != '0'
        if props.find(WORD+'outlineLvl') is not None:
            return props.find(WORD+'outlineLvl').get(WORD+'val') != '9'
        style = props.find(WORD+'pStyle')
        style = style.get(WORD+'val') if style is not None else None
    return styles(style)


def _style_lookup(styles_xml):
    """Funkcja: id stylu akapitu (None = domyślny) → czy to nagłówek lub styl listy."""
    if styles_xml is None:
        return lambda style_id: False
    root = etree.fromstring(styles_xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    styles, default = {}, None
    for style in root.iter(WORD+'style'):
        if style.get(WORD+'type') != 'paragraph':
            continue
        style_id = style.get(WORD+'styleId')
        name = style.find(WORD+'name')
        name = name.get(WORD+'val', '') if name is not None else ''
        based = style.find(WORD+'basedOn')
        props = style.find(WORD+'pPr')
        level = props.find(WORD+'outlineLvl') if props is not None else None
        numbering = props.find(WORD+'numPr') if props is not None else None
        own = (bool(HEADING_NAME.match(name) or HEADING_NAME.match(style_id or ''))
               or (level is not None and level.get(WORD+'val') != '9')
               or (numbering is not None and numbering.find(WORD+'numId') is not None
                   and numbering.find(WORD+'numId').get(WORD+'val') != '0'))
        styles[style_id] = (based.get(WORD+'val') if based is not None else None, own)
        if style.get(WORD+'default') in ('1', 'true', 'on'):
            default = style_id
    cache = {}

    def lookup(style_id):
        style_id = style_id or default
        if style_id not in cache:
            seen, current, found = set(), style_id, False
            while current in styles and current not in seen and not found:
                seen.add(current)
                current, found = styles[current]
            cache[style_id] = found
        return cache[style_id]

    return lookup


def _paragraphs_from_soft_breaks(root, ids, styles=lambda style_id: False):
    # Innermost paragraphs first, including text boxes, tables and footnotes.
    for paragraph in reversed(list(root.iter(WORD+'p'))):
        if _keeps_lines(paragraph, styles):
            continue
        breaks = [node for node in paragraph.iter() if (_soft_break(node)
                  or node.tag == WORD+'t' and '\u2028' in (node.text or ''))
                  and not any(parent.tag in DELETED for parent in node.iterancestors())]
        if not breaks:
            continue
        parts = _split_inline(paragraph, ids, paragraph=True)
        for index, part in enumerate(parts):
            if index:
                for key in tuple(part.attrib):
                    if etree.QName(key).localname in ('paraId', 'textId'):
                        del part.attrib[key]
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
        styles = _style_lookup(archive.read('word/styles.xml') if 'word/styles.xml' in names else None)
        for entry in archive.infolist():
            data = archive.read(entry)
            if PART.fullmatch(entry.filename):
                root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                for node in root.iter():
                    if node.tag == WORD+'t':
                        text = node.text or ''
                        normalized = text.translate(NONBREAKING)
                        if normalized != text:
                            node.text = normalized
                            node.set(SPACE, 'preserve')
                _paragraphs_from_soft_breaks(root, ids, styles)
                data = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
            target.writestr(entry, data)
    output.seek(0)
    return output
