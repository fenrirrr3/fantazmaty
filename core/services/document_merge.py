"""Scalanie przebudowanych dokumentów DOCX w jeden plik (Programy → Scalanie DOCX).

Wejściem są wyłącznie wyniki przebudowy (document_rebuild) – nowe dokumenty z szablonu
python-docx z neutralnymi stylami i formatowaniem bezpośrednim. Przebudowa nie przenosi
tabel, komentarzy, obrazów, hiperłączy ani automatycznej numeracji list, więc wystarczy
dołączyć akapity kolejnych dokumentów do pierwszego: style, numeracja i pozostałe części
pierwszego dokumentu obsługują całość. Przypisy dolne i końcowe trafiają do części przypisów
wyniku z nowymi numerami (w:id) w kolejności plików; gdy pierwszy plik nie ma przypisów,
część, jej relację, ustawienia i style przypisów bierzemy z pierwszego pliku, który je ma.
Identyfikatory śledzonych zmian i zakładek (w:id) dostają nowe, unikalne wartości. Każdy
kolejny plik zaczyna się od nowej strony.
Elementy, których przebudowa nie tworzy (odwołania do innych części pakietu), przerywają
scalanie zamiast dać uszkodzony dokument.
"""
from copy import deepcopy
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from .document_rebuild import SETTINGS_AFTER_NOTES

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_blank_text=False)
DOCUMENT = 'word/document.xml'
PACKAGE_RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
DOCUMENT_RELS = 'word/_rels/document.xml.rels'
# Odwołania do komentarzy i numeracji – przebudowa ich nie tworzy, scalanie ich nie przenosi.
FOREIGN = ('commentReference', 'numPr')
NOTE_KINDS = ('footnote', 'endnote')
NOTE_REFERENCES = {f'{{{W}}}{kind}Reference': kind for kind in NOTE_KINDS}
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
                    raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (bez komentarzy '
                                     'i numeracji list).')
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
            if not isinstance(node.tag, str) or node.get(w('id')) is None or node.tag in NOTE_REFERENCES:
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


def _xml(root):
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def _note_parts(archive):
    """Rodzaj przypisów → ścieżka części w pakiecie (z relacji dokumentu)."""
    if DOCUMENT_RELS not in archive.namelist():
        return {}
    found = {}
    for relationship in etree.fromstring(archive.read(DOCUMENT_RELS), PARSER):
        for kind in NOTE_KINDS:
            if (relationship.get('Type', '').endswith(f'/{kind}s') and relationship.get('TargetMode') != 'External'
                    and relationship.get('Target', '').strip('/')):
                target = relationship.get('Target').lstrip('/')
                found[kind] = target if target.startswith('word/') else 'word/' + target
    return found


class _Notes:
    """Przypisy scalanych plików: nowe w:id w kolejności plików, treść przypisów z unikalnymi w:id zmian."""

    def __init__(self):
        self.notes = {kind: [] for kind in NOTE_KINDS}
        self.donors = {}

    def collect(self, payload, elements, next_id):
        """Przypisy, do których odsyłają elementy pliku; zwraca kolejny wolny w:id zmian i zakładek."""
        with ZipFile(BytesIO(payload)) as archive:
            paths = _note_parts(archive)
            available = {}
            for kind, path in paths.items():
                if path not in archive.namelist():
                    continue
                root = etree.fromstring(archive.read(path), PARSER)
                self.donors.setdefault(kind, (payload, path))
                available[kind] = {note.get(w('id')): note for note in root.findall(w(kind))
                                   if note.get(w('type'), 'normal') == 'normal'}
        for element in elements:
            for node in element.iter(*NOTE_REFERENCES):
                kind = NOTE_REFERENCES[node.tag]
                note = available.get(kind, {}).get(node.get(w('id')))
                if note is None:
                    raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (odsyłacz przypisu '
                                     'bez treści przypisu).')
                note = deepcopy(note)
                next_id = _renumber(list(note), next_id)
                self.notes[kind].append(note)
                number = str(len(self.notes[kind]))
                node.set(w('id'), number)
                note.set(w('id'), number)
        return next_id

    def write(self, files, base):
        """Części przypisów wyniku (pliki pakietu base); brakującą część z relacją, ustawieniami
        i stylami bierzemy od dawcy."""
        with ZipFile(BytesIO(base)) as archive:
            paths = _note_parts(archive)
        for kind in NOTE_KINDS:
            if not self.notes[kind]:
                continue
            payload, donor_path = self.donors[kind]
            path = paths.get(kind)
            if path is None or path not in files:
                path = self._add_part(files, kind, payload, donor_path)
            root = etree.fromstring(files[path], PARSER)
            for note in root.findall(w(kind)):
                if note.get(w('type'), 'normal') == 'normal':
                    root.remove(note)
            root.extend(self.notes[kind])
            files[path] = _xml(root)

    def _add_part(self, files, kind, payload, donor_path):
        with ZipFile(BytesIO(payload)) as archive:
            donor = {name: archive.read(name) for name in (donor_path, '[Content_Types].xml', 'word/settings.xml',
                                                            'word/styles.xml') if name in archive.namelist()}
        path = donor_path
        files[path] = donor[path]
        types = etree.fromstring(files['[Content_Types].xml'], PARSER)
        types.append(etree.Element(f'{{{CONTENT_TYPES}}}Override', PartName='/' + path, ContentType=(
            f'application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}s+xml')))
        files['[Content_Types].xml'] = _xml(types)
        relationships = etree.fromstring(files[DOCUMENT_RELS], PARSER)
        taken = {node.get('Id') for node in relationships}
        number = len(taken) + 1
        while f'rId{number}' in taken:
            number += 1
        relationships.append(etree.Element(
            f'{{{PACKAGE_RELS}}}Relationship', Id=f'rId{number}', Target=path.removeprefix('word/'),
            Type=f'http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}s'))
        files[DOCUMENT_RELS] = _xml(relationships)
        if 'word/settings.xml' in donor and 'word/settings.xml' in files:
            properties = etree.fromstring(donor['word/settings.xml'], PARSER).find(w(kind + 'Pr'))
            settings = etree.fromstring(files['word/settings.xml'], PARSER)
            if properties is not None and settings.find(w(kind + 'Pr')) is None:
                anchor = next((node for node in settings if node.tag in SETTINGS_AFTER_NOTES), None)
                if anchor is None:
                    settings.append(properties)
                else:
                    anchor.addprevious(properties)
                files['word/settings.xml'] = _xml(settings)
        if 'word/styles.xml' in donor and 'word/styles.xml' in files:
            self._add_styles(files, etree.fromstring(donor['word/styles.xml'], PARSER), kind)
        return path

    def _add_styles(self, files, donor, kind):
        """Style przypisów (tekst i odsyłacz) z łańcuchem w:basedOn, których wynik jeszcze nie ma."""
        styles = etree.fromstring(files['word/styles.xml'], PARSER)
        known = {style.get(w('styleId')) for style in styles.findall(w('style'))}
        offered = {style.get(w('styleId')): style for style in donor.findall(w('style'))}
        wanted = [value for note in self.notes[kind] for node in note.iter(w('pStyle'), w('rStyle'))
                  for value in (node.get(w('val')),)]
        while wanted:
            style_id = wanted.pop()
            if style_id in known or style_id not in offered:
                continue
            known.add(style_id)
            styles.append(deepcopy(offered[style_id]))
            wanted.extend(node.get(w('val')) for node in offered[style_id].findall(w('basedOn')))
        files['word/styles.xml'] = _xml(styles)


def merge_documents(parts):
    """Jeden DOCX (bajty) z listy przebudowanych DOCX (bajty), w podanej kolejności."""
    if not parts:
        raise MergeError('Brak dokumentów do scalenia.')
    root = _document(parts[0])
    body, merged, section = _content(root)
    _check(merged)
    notes = _Notes()
    next_id = notes.collect(parts[0], merged, _highest_id(merged) + 1)
    for payload in parts[1:]:
        _, elements, _ = _content(_document(payload))
        elements = [deepcopy(element) for element in elements]
        _check(elements)
        next_id = _renumber(elements, next_id)
        next_id = notes.collect(payload, elements, next_id)
        merged.extend(_start_on_new_page(elements))
    for child in list(body):
        body.remove(child)
    body.extend(merged)
    if section is not None:
        body.append(section)
    with ZipFile(BytesIO(parts[0])) as source:
        files = {item.filename: source.read(item.filename) for item in source.infolist()}
        order = [item.filename for item in source.infolist()]
    files[DOCUMENT] = _xml(root)
    notes.write(files, parts[0])
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for name in order + [name for name in files if name not in order]:
            target.writestr(name, files[name], compress_type=ZIP_DEFLATED)
    return output.getvalue()
