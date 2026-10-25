"""Scalanie przebudowanych dokumentów DOCX w jeden plik (Programy → Scalanie DOCX).

Wejściem są wyłącznie wyniki przebudowy (document_rebuild) – nowe dokumenty z szablonu
python-docx z neutralnymi stylami i formatowaniem bezpośrednim. Przebudowa nie przenosi
tabel, obrazów, hiperłączy ani automatycznej numeracji list, więc wystarczy dołączyć akapity
kolejnych dokumentów do pierwszego: style, numeracja i pozostałe części pierwszego dokumentu
obsługują całość. Przypisy dolne i końcowe trafiają do części przypisów wyniku z nowymi
numerami (w:id) w kolejności plików; gdy pierwszy plik nie ma przypisów, część, jej relację,
ustawienia i style przypisów bierzemy z pierwszego pliku, który je ma. Komentarze (z odpowiedziami
i stanem „rozwiązany” z commentsExtended) trafiają do części komentarzy wyniku tak samo – z nowymi
w:id od 0 i nowymi w14:paraId akapitów komentarzy. Śledzone zmiany, zakładki i zakresy przeniesień
dostają nowe w:id, unikalne w całym wyniku (za komentarzami); pary (początek i koniec zakładki,
zakresu przeniesienia i komentarza) – wspólny numer, a nazwy przeniesień są unikalne. Każdy
kolejny plik zaczyna się od nowej strony.
Elementy, których przebudowa nie tworzy (odwołania do innych części pakietu), przerywają
scalanie zamiast dać uszkodzony dokument.
"""
from copy import deepcopy
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from .document_rebuild import COMMENTS_EXTENDED, SETTINGS_AFTER_NOTES, W15
from .document_xml import MC, R, W, mc, parse_part, serialize_part, w

W14 = '{http://schemas.microsoft.com/office/word/2010/wordml}'
DOCUMENT = 'word/document.xml'
PACKAGE_RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
DOCUMENT_RELS = 'word/_rels/document.xml.rels'
WORD_PART = 'application/vnd.openxmlformats-officedocument.wordprocessingml.{}+xml'
COMMENTS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments'
NOTE_KINDS = ('footnote', 'endnote')
NOTE_REFERENCES = {f'{{{W}}}{kind}Reference': kind for kind in NOTE_KINDS}
# Elementy właściwości akapitu, które w schemacie stoją przed w:pageBreakBefore.
BEFORE_PAGE_BREAK = {f'{{{W}}}{name}' for name in ('pStyle', 'keepNext', 'keepLines')}
BOOKMARKS = {f'{{{W}}}bookmarkStart', f'{{{W}}}bookmarkEnd'}
COMMENT_MARKERS = {f'{{{W}}}{name}' for name in ('commentRangeStart', 'commentRangeEnd', 'commentReference')}
MOVE_RANGES = {f'{{{W}}}{kind}Range{edge}': kind for kind in ('moveFrom', 'moveTo') for edge in ('Start', 'End')}
# Znaczniki bez treści między akapitami: nowa strona zaczyna się od pierwszego akapitu za nimi.
RANGE_MARKERS = BOOKMARKS | {f'{{{W}}}{name}' for name in ('commentRangeStart', 'commentRangeEnd')} | set(MOVE_RANGES)


class MergeError(ValueError):
    pass


class _Package:
    """Scalany plik otwarty raz: archiwum, ścieżki części z relacji dokumentu (typ relacji → ścieżka)
    i wczytane już części poboczne (przypisy, komentarze – tylko do odczytu).

    document() czyta treść za każdym razem od nowa: drzewa treści wszystkich plików naraz zajęłyby
    za dużo pamięci.
    """

    def __init__(self, payload):
        self.archive = ZipFile(BytesIO(payload))
        self.names = set(self.archive.namelist())
        self.paths = _part_paths(self.read(DOCUMENT_RELS))
        self._parts = {}

    def read(self, name):
        """Bajty części albo None, gdy jej nie ma."""
        return self.archive.read(name) if name in self.names else None

    def document(self):
        return parse_part(self.archive.read(DOCUMENT))

    def tree(self, path):
        """Drzewo części (wczytane raz); None, gdy części nie ma w pakiecie."""
        if path not in self.names:
            return None
        if path not in self._parts:
            self._parts[path] = parse_part(self.archive.read(path))
        return self._parts[path]

    def part(self, reltype):
        """Drzewo części o danym typie relacji; None, gdy relacji albo części brak."""
        return self.tree(self.paths.get(reltype))

    def note_parts(self):
        """Rodzaj przypisów → ścieżka części w pakiecie."""
        return {kind: path for kind in NOTE_KINDS for reltype, path in self.paths.items()
                if reltype.endswith(f'/{kind}s')}


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
            # Numeracja list – przebudowa jej nie tworzy, scalanie jej nie przenosi.
            if node.tag == w('numPr') and node.xpath('./w:numId[@w:val!="0"]', namespaces={'w': W}):
                raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (bez numeracji list).')
            if any(etree.QName(name).namespace == R for name in node.attrib):
                raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (bez obrazów i hiperłączy).')


class _Ids:
    """Nowe w:id scalanego dokumentu: komentarze od 0 (_Comments), pozostałe (śledzone zmiany, zmiany
    formatowania, zakładki, zakresy przeniesień) – kolejne liczby za komentarzami wszystkich plików."""

    def __init__(self, start):
        self.next = start
        self.names = set()

    def file(self, comments):
        """Numeracja kolejnego pliku: jego pary (zakładki, zakresy przeniesień) i nazwy przeniesień są osobne."""
        self.comments, self.pairs, self.renamed = comments, {}, {}

    def take(self):
        self.next += 1
        return str(self.next - 1)

    def name(self, old):
        """Unikalna nazwa przeniesienia (moveFrom i moveTo pliku łączy ta sama nazwa)."""
        if old not in self.renamed:
            new, number = old, 2
            while new in self.names:
                new, number = f'{old}_{number}', number + 1
            self.names.add(new)
            self.renamed[old] = new
        return self.renamed[old]

    def renumber(self, elements):
        for element in elements:
            for node in element.iter():
                if not isinstance(node.tag, str) or node.get(w('id')) is None or node.tag in NOTE_REFERENCES:
                    continue
                old = node.get(w('id'))
                if node.tag in COMMENT_MARKERS:
                    node.set(w('id'), self.comments.number(old))
                    continue
                key = 'bookmark' if node.tag in BOOKMARKS else MOVE_RANGES.get(node.tag)
                if key is None:
                    node.set(w('id'), self.take())
                    continue
                if (key, old) not in self.pairs:
                    self.pairs[key, old] = self.take()
                node.set(w('id'), self.pairs[key, old])
                if key != 'bookmark' and node.get(w('name')) is not None:
                    node.set(w('name'), self.name(node.get(w('name'))))


def _start_on_new_page(elements):
    """Pierwszy akapit pliku zaczyna nową stronę (bez pustego akapitu; znaczniki zakresów przed nim zostają);
    przed tabelą – akapit z podziałem. Poprzednie formatowanie zmiany (w:pPrChange) też ma podział, żeby
    odrzucenie zmiany go nie usuwało."""
    first = next((element for element in elements if element.tag not in RANGE_MARKERS), None)
    if first is None or first.tag != w('p'):
        paragraph = etree.Element(w('p'))
        etree.SubElement(etree.SubElement(paragraph, w('pPr')), w('pageBreakBefore'))
        index = elements.index(first) if first is not None else len(elements)
        return [*elements[:index], paragraph, *elements[index:]]
    properties = first.find(w('pPr'))
    if properties is None:
        properties = etree.Element(w('pPr'))
        first.insert(0, properties)
    for owner in (properties, properties.find(f'{w("pPrChange")}/{w("pPr")}')):
        if owner is None:
            continue
        for node in owner.findall(w('pageBreakBefore')):
            owner.remove(node)
        index = 0
        while index < len(owner) and owner[index].tag in BEFORE_PAGE_BREAK:
            index += 1
        owner.insert(index, etree.Element(w('pageBreakBefore')))
    return elements


def _part_paths(rels):
    """Typ relacji → ścieżka części w pakiecie (z relacji dokumentu: bajty albo None)."""
    found = {}
    for relationship in parse_part(rels) if rels is not None else ():
        target = relationship.get('Target', '')
        if relationship.get('TargetMode') != 'External' and target.strip('/'):
            target = target.lstrip('/')
            found.setdefault(relationship.get('Type', ''), target if target.startswith('word/') else 'word/' + target)
    return found


def _relate(files, path, content_type, reltype):
    """Nowa część dokumentu: wpis w [Content_Types].xml i relacja z wolnym rId."""
    types = parse_part(files['[Content_Types].xml'])
    types.append(etree.Element(f'{{{CONTENT_TYPES}}}Override', PartName='/' + path, ContentType=content_type))
    files['[Content_Types].xml'] = serialize_part(types)
    relationships = parse_part(files[DOCUMENT_RELS])
    taken = {node.get('Id') for node in relationships}
    number = len(taken) + 1
    while f'rId{number}' in taken:
        number += 1
    relationships.append(etree.Element(f'{{{PACKAGE_RELS}}}Relationship', Id=f'rId{number}',
                                       Target=path.removeprefix('word/'), Type=reltype))
    files[DOCUMENT_RELS] = serialize_part(relationships)


def _add_styles(files, donor, wanted):
    """Style (z łańcuchem w:basedOn) o identyfikatorach wanted, których wynik jeszcze nie ma, z części stylów
    dawcy (bajty); bez części stylów po którejś stronie – nic."""
    if donor is None or 'word/styles.xml' not in files:
        return
    styles = parse_part(files['word/styles.xml'])
    known = {style.get(w('styleId')) for style in styles.findall(w('style'))}
    offered = {style.get(w('styleId')): style for style in parse_part(donor).findall(w('style'))}
    wanted = list(wanted)
    while wanted:
        style_id = wanted.pop()
        if style_id in known or style_id not in offered:
            continue
        known.add(style_id)
        styles.append(deepcopy(offered[style_id]))
        wanted.extend(node.get(w('val')) for node in offered[style_id].findall(w('basedOn')))
    files['word/styles.xml'] = serialize_part(styles)


class _Notes:
    """Przypisy scalanych plików: nowe w:id w kolejności plików, treść przypisów z unikalnymi w:id zmian."""

    def __init__(self):
        self.notes = {kind: [] for kind in NOTE_KINDS}
        self.donors = {}

    def collect(self, package, elements, ids):
        """Przypisy, do których odsyłają elementy pliku, z nowymi w:id zmian i komentarzy (ids)."""
        available = {}
        for kind, path in package.note_parts().items():
            root = package.tree(path)
            if root is None:
                continue
            self.donors.setdefault(kind, (package, path))
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
                ids.renumber(list(note))
                self.notes[kind].append(note)
                number = str(len(self.notes[kind]))
                node.set(w('id'), number)
                note.set(w('id'), number)

    def write(self, files, base):
        """Części przypisów wyniku (pliki pakietu base); brakującą część z relacją, ustawieniami
        i stylami bierzemy od dawcy."""
        paths = base.note_parts()
        for kind in NOTE_KINDS:
            if not self.notes[kind]:
                continue
            donor, donor_path = self.donors[kind]
            path = paths.get(kind)
            if path is None or path not in files:
                path = self._add_part(files, kind, donor, donor_path)
            root = parse_part(files[path])
            for note in root.findall(w(kind)):
                if note.get(w('type'), 'normal') == 'normal':
                    root.remove(note)
            root.extend(self.notes[kind])
            files[path] = serialize_part(root)

    def _add_part(self, files, kind, package, donor_path):
        donor = {name: package.archive.read(name) for name in (donor_path, 'word/settings.xml', 'word/styles.xml')
                 if name in package.names}
        path = donor_path
        files[path] = donor[path]
        _relate(files, path, WORD_PART.format(kind + 's'),
                f'http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}s')
        if 'word/settings.xml' in donor and 'word/settings.xml' in files:
            properties = parse_part(donor['word/settings.xml']).find(w(kind + 'Pr'))
            settings = parse_part(files['word/settings.xml'])
            if properties is not None and settings.find(w(kind + 'Pr')) is None:
                anchor = next((node for node in settings if node.tag in SETTINGS_AFTER_NOTES), None)
                if anchor is None:
                    settings.append(properties)
                else:
                    anchor.addprevious(properties)
                files['word/settings.xml'] = serialize_part(settings)
        # Style przypisów (tekst i odsyłacz), których wynik jeszcze nie ma.
        _add_styles(files, donor.get('word/styles.xml'),
                    [node.get(w('val')) for note in self.notes[kind] for node in note.iter(w('pStyle'), w('rStyle'))])
        return path


class _Comments:
    """Komentarze scalanych plików (comments.xml, commentsExtended.xml): nowe w:id od 0 w kolejności pierwszego
    odwołania, nowe w14:paraId akapitów komentarzy (unikalne także względem akapitów treści) oraz odpowiedzi
    (w15:paraIdParent) i stan „rozwiązany” (w15:done) przeniesione z nowymi paraId."""

    def __init__(self, taken):
        self.comments, self.extended, self.donor = [], [], None
        self.taken, self.para = set(taken), 0

    def _para_id(self):
        self.para += 1
        while f'{self.para:08X}' in self.taken:
            self.para += 1
        return f'{self.para:08X}'

    def file(self, package):
        """Komentarze kolejnego pliku: number(stary w:id) → nowy w:id; finish() przenosi jego wątki."""
        comments, extended = package.part(COMMENTS), package.part(COMMENTS_EXTENDED)
        source = {} if comments is None else {node.get(w('id')): node for node in comments.findall(w('comment'))}
        threads = [] if extended is None else list(extended.iter(W15 + 'commentEx'))
        return _FileComments(self, package, source, threads)


class _FileComments:
    def __init__(self, merged, package, source, threads):
        self.merged, self.package, self.source, self.threads = merged, package, source, threads
        self.numbers, self.paragraphs = {}, {}

    def number(self, old):
        if old not in self.numbers:
            comment = self.source.get(old)
            if comment is None:
                raise MergeError('Scalanie obsługuje tylko przebudowane dokumenty (odwołanie do komentarza '
                                 'bez treści komentarza).')
            comment = deepcopy(comment)
            _check([comment])
            self.numbers[old] = str(len(self.merged.comments))
            comment.set(w('id'), self.numbers[old])
            for paragraph in comment.iter(w('p')):
                if paragraph.get(W14 + 'paraId') is not None:
                    new = self.merged._para_id()
                    self.paragraphs[paragraph.get(W14 + 'paraId')] = new
                    paragraph.set(W14 + 'paraId', new)
            self.merged.comments.append(comment)
            self.merged.donor = self.merged.donor or self.package
        return self.numbers[old]

    def finish(self):
        """Wpisy commentsExtended przeniesionych komentarzy (odpowiedź na komentarz spoza wyniku – bez rodzica)."""
        for entry in self.threads:
            if entry.get(W15 + 'paraId') not in self.paragraphs:
                continue
            entry = deepcopy(entry)
            entry.set(W15 + 'paraId', self.paragraphs[entry.get(W15 + 'paraId')])
            parent = entry.attrib.pop(W15 + 'paraIdParent', None)
            if parent in self.paragraphs:
                entry.set(W15 + 'paraIdParent', self.paragraphs[parent])
            self.merged.extended.append(entry)


def _write_comments(files, base, comments):
    """Części komentarzy wyniku: nowe treści w częściach pierwszego pliku albo nowe części (z relacją
    i stylami komentarzy od dawcy)."""
    for reltype, kind, name, nsmap, entries in (
            (COMMENTS, 'comments', w('comments'), {'w': W, 'w14': W14[1:-1], 'mc': MC}, comments.comments),
            (COMMENTS_EXTENDED, 'commentsExtended', W15 + 'commentsEx', {'w15': W15[1:-1], 'mc': MC},
             comments.extended)):
        path = base.paths.get(reltype)
        if not entries and (path is None or path not in files):
            continue
        if path is None or path not in files:
            path = f'word/{kind}.xml'
            _relate(files, path, WORD_PART.format(kind), reltype)
        root = etree.Element(name, nsmap=nsmap)
        root.set(mc('Ignorable'), 'w14' if kind == 'comments' else 'w15')
        root.extend(entries)
        files[path] = serialize_part(root)
    if comments.comments:
        _add_styles(files, comments.donor.read('word/styles.xml'), {'CommentReference'} | {
            node.get(w('val')) for comment in comments.comments for node in comment.iter(w('pStyle'), w('rStyle'))})


def _survey(packages):
    """Liczba komentarzy wszystkich plików (pierwszy wolny w:id po nich) i w14:paraId akapitów treści i przypisów."""
    count, taken = 0, set()
    for package in packages:
        for reltype, path in package.paths.items():
            if path not in package.names:
                continue
            if reltype == COMMENTS:
                count += len(package.tree(path).findall(w('comment')))
            elif reltype.endswith(('/footnotes', '/endnotes')):
                taken.update(package.tree(path).xpath('//@w14:paraId', namespaces={'w14': W14[1:-1]}))
        taken.update(package.document().xpath('//@w14:paraId', namespaces={'w14': W14[1:-1]}))
    return count, taken


def merge_documents(parts):
    """Jeden DOCX (bajty) z listy przebudowanych DOCX (bajty), w podanej kolejności."""
    if not parts:
        raise MergeError('Brak dokumentów do scalenia.')
    packages = [_Package(payload) for payload in parts]
    base = packages[0]
    root = base.document()
    body, merged, section = _content(root)
    count, taken = _survey(packages)
    ids, notes, comments = _Ids(count), _Notes(), _Comments(taken)
    for index, package in enumerate(packages):
        if index:
            _, elements, _ = _content(package.document())
            elements = [deepcopy(element) for element in elements]
        else:
            elements = merged
        _check(elements)
        own = comments.file(package)
        ids.file(own)
        ids.renumber(elements)
        notes.collect(package, elements, ids)
        own.finish()
        if index:
            merged.extend(_start_on_new_page(elements))
    for child in list(body):
        body.remove(child)
    body.extend(merged)
    if section is not None:
        body.append(section)
    files = {item.filename: base.archive.read(item.filename) for item in base.archive.infolist()}
    files[DOCUMENT] = serialize_part(root)
    notes.write(files, base)
    _write_comments(files, base, comments)
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as target:
        # Nowe części trafiają na koniec, po częściach pierwszego pliku w ich kolejności.
        for name, data in files.items():
            target.writestr(name, data, compress_type=ZIP_DEFLATED)
    return output.getvalue()
