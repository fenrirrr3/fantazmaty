"""Replace soft line breaks with paragraphs and nonbreaking spaces with spaces."""
from copy import deepcopy
from io import BytesIO
import re
from zipfile import ZipFile
from lxml import etree

WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SPACE = '{http://www.w3.org/XML/1998/namespace}space'
PART = re.compile(r'word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml\Z')


def _soft_break(node):
    return node.tag == WORD+'cr' or (
        node.tag == WORD+'br' and node.get(WORD+'type', 'textWrapping') == 'textWrapping')


def _split_inline(node, *, paragraph=False):
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
    if node.tag == WORD+'p' and not paragraph:
        return [deepcopy(node)]
    if not len(node):
        return [deepcopy(node)]
    properties = {WORD+'pPr', WORD+'rPr', WORD+'sdtPr', WORD+'sdtEndPr'}

    def shell():
        result = etree.Element(node.tag, attrib=dict(node.attrib), nsmap=node.nsmap)
        result.text = node.text
        for child in node:
            if child.tag in properties:
                result.append(deepcopy(child))
        return result

    result = [shell()]
    for child in node:
        if child.tag in properties:
            continue
        parts = _split_inline(child)
        if parts[0] is not None:
            result[-1].append(parts[0])
        for part in parts[1:]:
            result.append(shell())
            if part is not None:
                result[-1].append(part)
    result[-1].tail = node.tail
    return result


def _paragraphs_from_soft_breaks(root):
    # Innermost paragraphs first, including text boxes, tables and footnotes.
    for paragraph in reversed(list(root.iter(WORD+'p'))):
        breaks = [node for node in paragraph.iter() if _soft_break(node)
                  or node.tag == WORD+'t' and '\u2028' in (node.text or '')]
        if not breaks:
            continue
        parts = _split_inline(paragraph, paragraph=True)
        if len(parts) == 1:
            continue
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
        for entry in archive.infolist():
            data = archive.read(entry)
            if PART.fullmatch(entry.filename):
                root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                for node in root.iter():
                    if node.tag == WORD+'t':
                        text = node.text or ''
                        normalized = text.replace('\u00a0', ' ').replace('\u202f', ' ')
                        if normalized != text:
                            node.text = normalized
                            node.set(SPACE, 'preserve')
                _paragraphs_from_soft_breaks(root)
                data = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
            target.writestr(entry, data)
    output.seek(0)
    return output
