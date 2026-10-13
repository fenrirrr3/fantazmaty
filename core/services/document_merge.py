"""Scalanie przebudowanych dokumentów DOCX w jeden plik (Programy → Scalanie DOCX).

Wejściem są wyłącznie wyniki przebudowy (document_rebuild) – nowe dokumenty z szablonu
python-docx z neutralnymi stylami i formatowaniem bezpośrednim. Przebudowa nie przenosi
tabel, przypisów, komentarzy, obrazów, hiperłączy ani automatycznej numeracji list, więc
wystarczy dołączyć akapity kolejnych dokumentów do pierwszego: style, numeracja i pozostałe
części pierwszego dokumentu obsługują całość. Identyfikatory śledzonych zmian i zakładek
(w:id) dostają nowe, unikalne wartości. Każdy kolejny plik zaczyna się od nowej strony.
Elementy, których przebudowa nie tworzy (odwołania do innych części pakietu), przerywają
scalanie zamiast dać uszkodzony dokument.
"""
from copy import deepcopy
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_blank_text=False)
DOCUMENT = 'word/document.xml'
# Odwołania do przypisów, komentarzy i numeracji – przebudowa ich nie tworzy, scalanie ich nie przenosi.
FOREIGN = ('footnoteReference', 'endnoteReference', 'commentReference', 'numPr')
# Elementy właściwości akapitu, które w schemacie stoją przed w:pageBreakBefore.
BEFORE_PAGE_BREAK = {f'{{{W}}}{name}' for name in ('pStyle', 'keepNext', 'keepLines')}
BOOKMARKS = {f'{{{W}}}bookmarkStart', f'{{{W}}}bookmarkEnd'}


class MergeError(ValueError):
    pass


def w(name):
    return f'{{{W}}}{name}'


def _document(payload):
    with ZipFile(BytesIO(payload)) as archive:
        return etree.fromstring(archive.read(DOCUMENT), PARSER)


def _content(root):
    """Elementy treści (bez końcowego w:sectPr) i sam w:sectPr dokumentu."""
    body = root.find(w('body'))
    if body is None:
        raise MergeError('Dokument nie ma treści.')
    children = list(body)
    section = children.pop() if children and children[-1].tag == w('sectPr') else None
    return body, children, section


def _check(elements):
    for element in elements:
        for node in element.iter():
            if not isinstance(node.tag, str):
                continue
            if etree.QName(node).localname in FOREIGN and etree.QName(node).namespace == W:
                if node.tag != w('numPr') or node.xpath('./w:numId[@w:val!="0"]', namespaces={'w': W}):
                    raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (bez przypisów, '
                                     'komentarzy i numeracji list).')
            if any(etree.QName(name).namespace == R for name in node.attrib):
                raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (bez obrazów i hiperłączy).')


def _highest_id(elements):
    values = [int(value) for element in elements for node in element.iter() if isinstance(node.tag, str)
              for value in (node.get(w('id')),) if value and value.lstrip('-').isdigit()]
    return max(values, default=0)


def _renumber(elements, start):
    """Nowe w:id (śledzone zmiany, zakładki) od start; zakładka zachowuje parę start–koniec."""
    next_id = start
    bookmarks = {}
    for element in elements:
        for node in element.iter():
            if not isinstance(node.tag, str) or node.get(w('id')) is None:
                continue
            if node.tag in BOOKMARKS:
                old = node.get(w('id'))
                if old not in bookmarks:
                    bookmarks[old] = next_id
                    next_id += 1
                node.set(w('id'), str(bookmarks[old]))
            else:
                node.set(w('id'), str(next_id))
                next_id += 1
    return next_id


def _start_on_new_page(elements):
    """Pierwszy akapit pliku zaczyna nową stronę (bez pustego akapitu); przed tabelą – akapit z podziałem."""
    first = elements[0] if elements else None
    if first is None or first.tag != w('p'):
        paragraph = etree.Element(w('p'))
        etree.SubElement(etree.SubElement(paragraph, w('pPr')), w('pageBreakBefore'))
        return [paragraph, *elements]
    properties = first.find(w('pPr'))
    if properties is None:
        properties = etree.Element(w('pPr'))
        first.insert(0, properties)
    for node in properties.findall(w('pageBreakBefore')):
        properties.remove(node)
    index = 0
    while index < len(properties) and properties[index].tag in BEFORE_PAGE_BREAK:
        index += 1
    properties.insert(index, etree.Element(w('pageBreakBefore')))
    return elements


def merge_documents(parts):
    """Jeden DOCX (bajty) z listy przebudowanych DOCX (bajty), w podanej kolejności."""
    if not parts:
        raise MergeError('Brak dokumentów do scalenia.')
    root = _document(parts[0])
    body, merged, section = _content(root)
    _check(merged)
    next_id = _highest_id(merged) + 1
    for payload in parts[1:]:
        _, elements, _ = _content(_document(payload))
        elements = [deepcopy(element) for element in elements]
        _check(elements)
        next_id = _renumber(elements, next_id)
        merged.extend(_start_on_new_page(elements))
    for child in list(body):
        body.remove(child)
    body.extend(merged)
    if section is not None:
        body.append(section)
    output = BytesIO()
    with ZipFile(BytesIO(parts[0])) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == DOCUMENT:
                data = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
            target.writestr(item, data, compress_type=ZIP_DEFLATED)
    return output.getvalue()
