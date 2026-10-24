"""Podział DOCX na opowiadania według akapitów ze stylem „Nagłówek 1” (Programy → Podział DOCX).

Działa w izolowanym podprocesie konwertera (document_conversion_worker), więc importuje
tylko bibliotekę standardową i lxml. Każdy Nagłówek 1 zaczyna opowiadanie, które trwa do
kolejnego Nagłówka 1; treść przed pierwszym nagłówkiem (np. strona tytułowa) jest pomijana.

Plik opowiadania to kopia pakietu z treścią ograniczoną do fragmentu i ostatnim w:sectPr:
style, numeracja, motyw i ustawienia zostają, a przypisy, komentarze, obrazy, hiperłącza
i nagłówki stron, do których fragment się nie odwołuje, są usuwane razem z nieużywanymi
częściami pakietu (także miniatura pierwszej strony) – plik nie niesie treści innych opowiadań.

Śledzone zmiany zostają bez zmian. Komentarz trafia do opowiadania ze swoim odwołaniem (także
w przypisie); zakres przecięty granicą opowiadań zaczyna się tam na początku opowiadania, a w innych
opowiadaniach jego znaczniki znikają. Zakres przeniesienia, którego drugiej połowy (albo drugiej
strony przeniesienia) nie ma w opowiadaniu, znika – przeniesiony tekst i jego oznaczenia zostają.
"""
import posixpath
import re
from copy import deepcopy
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
HEADING_NAMES = {'heading 1', 'nagłówek 1'}
HEADING_IDS = {'heading1', 'nagwek1'}
# Części, do których treść nie odwołuje się przez r:id – zostają zawsze (style, numeracja, ustawienia…).
STRUCTURAL = ('/styles', '/stylesWithEffects', '/numbering', '/settings', '/webSettings', '/fontTable',
              '/theme', '/footnotes', '/endnotes', '/comments', '/commentsExtended', '/commentsIds',
              '/commentsExtensible', '/people', '/customXml', '/glossaryDocument')
NOTE_TYPES = {'footnotes': 'footnote', 'endnotes': 'endnote'}
FORBIDDEN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
NAME_LIMIT = 80


class NoHeadingError(ValueError):
    pass


def w(name):
    return f'{{{W}}}{name}'


def _parse(data):
    return etree.fromstring(data, PARSER)


def _serialize(root):
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def _rels_path(part):
    folder, name = posixpath.split(part)
    return posixpath.join(folder, '_rels', name + '.rels')


def _target(part, relationship):
    target = relationship.get('Target', '')
    if target.startswith('/'):
        return posixpath.normpath(target.lstrip('/'))
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _internal(relationship):
    return relationship.get('TargetMode', '').lower() != 'external'


def _relationships(files, part):
    data = files.get(_rels_path(part))
    return _parse(data) if data is not None else None


def _main_part(files):
    """Główna część dokumentu z relacji pakietu; bez niej Word nie otworzy pliku, a wynik byłby pusty."""
    rels = _relationships(files, '')
    for relationship in rels if rels is not None else ():
        if relationship.get('Type', '').endswith('/officeDocument') and _internal(relationship):
            return _target('', relationship)
    raise ValueError('Pakiet DOCX nie wskazuje głównej części dokumentu.')


def _drop_thumbnail(files):
    """Miniatura pierwszej strony (docProps/thumbnail) pokazuje początek całego pliku – opowiadania jej nie dostają."""
    rels = _relationships(files, '')
    for relationship in list(rels):
        if relationship.get('Type', '').endswith('/metadata/thumbnail'):
            rels.remove(relationship)
    files[_rels_path('')] = _serialize(rels)


def _part_of_type(files, part, suffix):
    rels = _relationships(files, part)
    for relationship in rels if rels is not None else ():
        if relationship.get('Type', '').endswith(suffix) and _internal(relationship):
            return _target(part, relationship)
    return None


def heading_style_ids(styles_xml):
    """Identyfikatory stylów akapitu „Nagłówek 1” (nazwa stylu albo jego id, bez względu na wielkość liter)."""
    ids = set()
    if styles_xml is not None:
        for style in _parse(styles_xml).iter(w('style')):
            style_id = style.get(w('styleId')) or ''
            name = style.find(w('name'))
            name = (name.get(w('val')) if name is not None else '') or ''
            if name.casefold() in HEADING_NAMES or style_id.casefold() in HEADING_IDS:
                ids.add(style_id)
    return ids | {'Heading1', 'Nagwek1'}


def _is_heading(element, ids):
    if element.tag != w('p'):
        return False
    style = element.find(f'{w("pPr")}/{w("pStyle")}')
    return style is not None and style.get(w('val')) in ids


def _text(element):
    parts = []
    for node in element.iter(w('t'), w('tab')):
        parts.append(' ' if node.tag == w('tab') else node.text or '')
    return re.sub(r'\s+', ' ', ''.join(parts)).strip()


def _visible(elements):
    return any(_text(element) or element.find(f'.//{w("drawing")}') is not None
               or element.find(f'.//{w("pict")}') is not None for element in elements)


def file_name(number, width, title):
    """„01 Tytuł.docx”: tytuł bez znaków niedozwolonych w nazwach plików, skrócony."""
    clean = re.sub(r'\s+', ' ', FORBIDDEN_NAME.sub(' ', title)).strip(' .')
    clean = clean[:NAME_LIMIT].rstrip(' .') or 'Opowiadanie'
    return f'{number:0{width}d} {clean}.docx'


def _referenced(elements):
    """Identyfikatory użyte we fragmencie: relacje (r:*), przypisy, przypisy końcowe i komentarze."""
    found = {'rels': set(), 'footnotes': set(), 'endnotes': set(), 'comments': set()}
    for element in elements:
        for node in element.iter():
            if not isinstance(node.tag, str):
                continue
            for key, value in node.attrib.items():
                name = etree.QName(key)
                if name.namespace == R or name.localname == 'relid':
                    found['rels'].add(value)
            local = etree.QName(node).localname
            if local == 'footnoteReference':
                found['footnotes'].add(node.get(w('id')))
            elif local == 'endnoteReference':
                found['endnotes'].add(node.get(w('id')))
            elif local in ('commentReference', 'commentRangeStart', 'commentRangeEnd'):
                found['comments'].add(node.get(w('id')))
    return found


def _filter_rels(files, part, used):
    """Relacje części: zostają strukturalne i te, do których część się odwołuje."""
    rels = _relationships(files, part)
    if rels is None:
        return
    for relationship in list(rels):
        if relationship.get('Id') in used:
            continue
        if _internal(relationship) and relationship.get('Type', '').endswith(STRUCTURAL):
            continue
        rels.remove(relationship)
    files[_rels_path(part)] = _serialize(rels)


def _filter_notes(files, part, kind, used):
    """Tylko przypisy użyte we fragmencie; zwraca korzeń części (do odczytu odwołań do komentarzy)."""
    root = _parse(files[part])
    for note in list(root.findall(w(NOTE_TYPES[kind]))):
        if note.get(w('type')) in (None, 'normal') and note.get(w('id')) not in used:
            root.remove(note)
    files[part] = _serialize(root)
    _filter_rels(files, part, _referenced([root])['rels'])
    return root


COMMENT_RANGES = (w('commentRangeStart'), w('commentRangeEnd'))
MOVE_RANGES = {w(f'{kind}Range{edge}'): (kind, edge) for kind in ('moveFrom', 'moveTo') for edge in ('Start', 'End')}


def _review_ranges(content, notes):
    """Zakresy komentarzy i przeniesień przecięte granicą opowiadania (content – elementy treści, notes – korzenie
    użytych przypisów); zwraca w:id komentarzy z odwołaniem w opowiadaniu (te zostają w comments.xml)."""
    referenced = {node.get(w('id')) for root in [*content, *notes] for node in root.iter(w('commentReference'))}
    found = {}
    for element in content:
        for node in element.iter(*COMMENT_RANGES):
            found.setdefault(node.get(w('id')), {})[node.tag] = node
    first = next((element for element in content if element.tag == w('p')), None)
    for comment_id, nodes in found.items():
        if comment_id not in referenced:
            for node in nodes.values():
                node.getparent().remove(node)
        elif w('commentRangeStart') not in nodes and first is not None:
            # Zakres zaczęty w poprzednim opowiadaniu: tu obejmuje jego początek.
            start = etree.Element(w('commentRangeStart'))
            start.set(w('id'), comment_id)
            properties = first.find(w('pPr'))
            first.insert(0 if properties is None else first.index(properties) + 1, start)
        elif w('commentRangeEnd') not in nodes:
            reference = next((node for element in content for node in element.iter(w('commentReference'))
                              if node.get(w('id')) == comment_id), None)
            if reference is not None:
                end = etree.Element(w('commentRangeEnd'))
                end.set(w('id'), comment_id)
                reference.getparent().addprevious(end)
    moves = {}
    for element in content:
        for node in element.iter(*MOVE_RANGES):
            moves.setdefault((MOVE_RANGES[node.tag][0], node.get(w('id'))), []).append(node)
    names = {key: next((node.get(w('name')) for node in nodes if MOVE_RANGES[node.tag][1] == 'Start'), None)
             for key, nodes in moves.items()}
    complete = {(key[0], names[key]) for key, nodes in moves.items()
                if sorted(MOVE_RANGES[node.tag][1] for node in nodes) == ['End', 'Start']}
    other = {'moveFrom': 'moveTo', 'moveTo': 'moveFrom'}
    for key, nodes in moves.items():
        if (key[0], names[key]) not in complete or (other[key[0]], names[key]) not in complete:
            for node in nodes:
                node.getparent().remove(node)
    return referenced


def _local_attribute(node, name):
    return next((value for key, value in node.attrib.items() if etree.QName(key).localname == name), None)


def _filter_comments(files, main, used):
    part = _part_of_type(files, main, '/comments')
    if part is None or part not in files:
        return
    root = _parse(files[part])
    for comment in list(root.findall(w('comment'))):
        if comment.get(w('id')) not in used:
            root.remove(comment)
    files[part] = _serialize(root)
    _filter_rels(files, part, _referenced([root])['rels'])
    paragraphs = {_local_attribute(node, 'paraId') for node in root.iter(w('p'))} - {None}
    durable = set()
    for suffix, key in (('/commentsExtended', 'paraId'), ('/commentsIds', 'paraId')):
        extra = _part_of_type(files, main, suffix)
        if extra is None or extra not in files:
            continue
        tree = _parse(files[extra])
        for node in list(tree):
            if isinstance(node.tag, str) and _local_attribute(node, key) not in paragraphs:
                tree.remove(node)
            elif suffix == '/commentsIds':
                durable.add(_local_attribute(node, 'durableId'))
            elif isinstance(node.tag, str):
                # Odpowiedź na komentarz z innego opowiadania staje się zwykłym komentarzem.
                for name in [name for name in node.attrib if etree.QName(name).localname == 'paraIdParent']:
                    if node.get(name) not in paragraphs:
                        del node.attrib[name]
        files[extra] = _serialize(tree)
    extensible = _part_of_type(files, main, '/commentsExtensible')
    if extensible is not None and extensible in files:
        tree = _parse(files[extensible])
        for node in list(tree):
            if isinstance(node.tag, str) and etree.QName(node).localname == 'commentExtensible' \
                    and _local_attribute(node, 'durableId') not in durable:
                tree.remove(node)
        files[extensible] = _serialize(tree)


def _reachable(files):
    """Części osiągalne z relacji pakietu (pozostałe są usuwane z pliku wynikowego)."""
    seen, queue = set(), ['']
    while queue:
        part = queue.pop()
        rels = _relationships(files, part)
        if rels is None:
            continue
        seen.add(_rels_path(part))
        for relationship in rels:
            if _internal(relationship):
                target = _target(part, relationship)
                if target in files and target not in seen:
                    seen.add(target)
                    queue.append(target)
    return seen | {'[Content_Types].xml'}


def _package(files, order, compress):
    keep = _reachable(files)
    types = _parse(files['[Content_Types].xml'])
    for override in list(types.findall(f'{{{CONTENT_TYPES}}}Override')):
        if override.get('PartName', '').lstrip('/') not in keep:
            types.remove(override)
    files['[Content_Types].xml'] = _serialize(types)
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name in order:
            if name in keep:
                archive.writestr(name, files[name], compress_type=compress.get(name, ZIP_DEFLATED))
    return output.getvalue()


def split_document(source, output):
    """Dzieli DOCX (ścieżka lub strumień) na pliki opowiadań zapisane jako ZIP w output (strumień).

    Zwraca {"files": [nazwy plików], "titles": [teksty nagłówków], "skipped_intro": bool};
    bez Nagłówka 1 zgłasza NoHeadingError. Tytuły służą dopasowaniu plików do tekstów antologii.
    """
    with ZipFile(source) as archive:
        order = [item.filename for item in archive.infolist() if not item.is_dir()]
        compress = {item.filename: item.compress_type for item in archive.infolist()}
        original = {name: archive.read(name) for name in order}
    main = _main_part(original)
    _drop_thumbnail(original)
    styles = _part_of_type(original, main, '/styles')
    ids = heading_style_ids(original.get(styles) if styles else None)
    root = _parse(original[main])
    body = root.find(w('body'))
    children = list(body) if body is not None else []
    section = children.pop() if children and children[-1].tag == w('sectPr') else None
    stories, intro = [], []
    for element in children:
        if _is_heading(element, ids):
            stories.append([element])
        elif stories:
            stories[-1].append(element)
        else:
            intro.append(element)
    if not stories:
        raise NoHeadingError('Brak akapitów ze stylem Nagłówek 1.')
    for element in children:
        body.remove(element)
    width = max(2, len(str(len(stories))))
    names, titles = [], []
    with ZipFile(output, 'w', ZIP_STORED) as result:
        for number, elements in enumerate(stories, 1):
            content = [deepcopy(element) for element in elements]
            if section is not None:
                body.remove(section)
            body.extend(content)
            if section is not None:
                body.append(section)
            files = dict(original)
            used = _referenced(content + ([section] if section is not None else []))
            _filter_rels(files, main, used['rels'])
            notes = []
            for kind in NOTE_TYPES:
                part = _part_of_type(files, main, '/' + kind)
                if part is not None and part in files:
                    notes.append(_filter_notes(files, part, kind, used[kind]))
            _filter_comments(files, main, _review_ranges(content, notes))
            files[main] = _serialize(root)
            title = _text(elements[0])
            name = file_name(number, width, title)
            names.append(name)
            titles.append(title)
            result.writestr(name, _package(files, order, compress))
            for element in content:
                if element.getparent() is body:  # znacznik zakresu między akapitami mógł już zniknąć
                    body.remove(element)
    return {'files': names, 'titles': titles, 'skipped_intro': _visible(intro)}
