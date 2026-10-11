"""Obrazy i język dokumentu przed wszelkimi zmianami (wszystkie programy).

strip_images – usuwa obrazy z każdej części dokumentu (treść, nagłówki, stopki,
przypisy, komentarze, pola tekstowe, numeracja z obrazkowymi punktorami) razem
z nieużywanymi plikami mediów, ich relacjami i wpisami [Content_Types].xml.
force_polish – oznacza cały dokument jako polski (każde w:lang w:val = pl-PL,
także w stylach i ustawieniach domyślnych).

Moduł działa w izolowanym podprocesie konwertera: tylko biblioteka standardowa i lxml.
"""
import posixpath
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

POLISH = 'pl-PL'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PIC = 'http://schemas.openxmlformats.org/drawingml/2006/picture'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
V = 'urn:schemas-microsoft-com:vml'
OFFICE = 'urn:schemas-microsoft-com:office:office'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)

# Relacje prowadzące do obrazów i osadzonych obiektów: po usunięciu odwołań w treści znikają.
MEDIA_RELATIONSHIPS = ('/image', '/oleObject', '/package', '/hdphoto', '/media', '/video', '/audio')
# Miniatura pakietu (podgląd pierwszej strony w eksploratorze plików) pokazywałaby usunięte obrazy.
THUMBNAIL_RELATIONSHIP = '/metadata/thumbnail'
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'jpe', 'gif', 'bmp', 'dib', 'tif', 'tiff', 'emf', 'wmf', 'emz', 'wmz',
                    'svg', 'webp', 'pict', 'jfif', 'wdp', 'ico'}
# Skrót, czy część w ogóle może zawierać obraz (bez parsowania dużych plików XML).
IMAGE_MARKERS = (b'drawing', b'pict', b'object', b'imagedata', b'blip', b'background', b'PicBullet', b'fill')
# Kolejność elementów w w:rPr (CT_RPr) – w:lang musi stać przed tymi elementami.
AFTER_LANG = {f'{{{W}}}{name}' for name in ('eastAsianLayout', 'specVanish', 'oMath', 'rPrChange')}


def _q(namespace, name):
    return f'{{{namespace}}}{name}'


def _read(source):
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    source.seek(0)
    return source.read()


def _is_xml(name):
    return name.lower().endswith(('.xml', '.rels'))


def _serialize(root):
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def _rels_name(part):
    folder, name = posixpath.split(part)
    return posixpath.join(folder, '_rels', name + '.rels')


def _source_part(rels_name):
    """word/_rels/document.xml.rels → word/document.xml; _rels/.rels → ''."""
    folder, name = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(folder), name[:-len('.rels')])


def _target(rels_name, relationship):
    target = relationship.get('Target', '')
    if relationship.get('TargetMode', '').lower() == 'external':
        return None
    if target.startswith('/'):
        return posixpath.normpath(target.lstrip('/'))
    base = posixpath.dirname(_source_part(rels_name))
    return posixpath.normpath(posixpath.join(base, target))


def _write(original, parts, removed=()):
    output = BytesIO()
    with ZipFile(BytesIO(original)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            if item.filename in removed:
                continue
            data = parts.get(item.filename)
            target.writestr(item, source.read(item.filename) if data is None else data,
                            compress_type=ZIP_DEFLATED)
    output.seek(0)
    return output


def _remove(node):
    parent = node.getparent()
    if parent is not None:
        # Tekst „ogona” elementu (tylko w nietypowym XML) zostaje przy poprzedniku.
        if node.tail and node.tail.strip():
            previous = node.getprevious()
            if previous is not None:
                previous.tail = (previous.tail or '') + node.tail
            else:
                parent.text = (parent.text or '') + node.tail
        parent.remove(node)


def _counted(node):
    """Obraz w wersji zapasowej (mc:Fallback) to ten sam obraz co w mc:Choice – liczymy go raz."""
    return not any(ancestor.tag == _q(MC, 'Fallback') for ancestor in node.iterancestors())


def _has_blip(node):
    return any(True for _ in node.iter(_q(A, 'blip'), _q(V, 'imagedata')))


def _strip_part(root):
    """Usuwa obrazy z jednej części XML; zwraca liczbę usuniętych obrazów."""
    count = 0
    # Obrazkowe punktory list: poziom listy wraca do zwykłego znaku punktora (w:lvlText).
    bullets = list(root.iter(_q(W, 'numPicBullet')))
    if bullets:
        for node in root.iter(_q(W, 'lvlPicBulletId')):
            _remove(node)
        for node in bullets:
            _remove(node)
        count += len(bullets)
    # Tło strony z obrazem (Projektowanie → Kolor strony → Efekty wypełnienia).
    for node in list(root.iter(_q(W, 'background'))):
        if _has_blip(node) or any(fill.get(_q(R, 'id')) for fill in node.iter(_q(V, 'fill'))):
            _remove(node)
            count += 1
    # DrawingML: obraz (pic:pic) jako cały rysunek, obrazy w grupach i obrazkowe wypełnienia kształtów.
    # Od końca: rysunek wewnątrz pola tekstowego jest obsłużony przed samym polem.
    for drawing in reversed(list(root.iter(_q(W, 'drawing')))):
        if drawing.getparent() is None:
            continue
        data = drawing.find(f'.//{_q(A, "graphicData")}')
        if data is not None and data.get('uri', '').endswith('/picture'):
            count += _counted(drawing)
            _remove(drawing)
            continue
        for picture in list(drawing.iter(_q(PIC, 'pic'))):
            count += _counted(picture)
            _remove(picture)
        for fill in list(drawing.iter(_q(A, 'blipFill'))):
            count += _counted(fill)
            fill.addnext(etree.Element(_q(A, 'noFill')))
            _remove(fill)
    # Obiekty osadzone (OLE) z obrazem podglądu – znikają w całości.
    for node in list(root.iter(_q(W, 'object'))):
        if node.getparent() is not None:
            count += _counted(node)
            _remove(node)
    # VML: kształt z obrazem znika; pole tekstowe VML traci tylko obraz (tekst zostaje).
    for pict in reversed(list(root.iter(_q(W, 'pict')))):
        if pict.getparent() is None:
            continue
        images = [node for node in pict.iter(_q(V, 'imagedata'))]
        fills = [node for node in pict.iter(_q(V, 'fill')) if node.get(_q(R, 'id')) or node.get(_q(OFFICE, 'relid'))]
        if not images and not fills:
            continue
        if not any(True for _ in pict.iter(_q(V, 'textbox'), _q(W, 'txbxContent'))):
            count += _counted(pict)
            _remove(pict)
            continue
        for node in images + fills:
            count += _counted(node)
            _remove(node)
    # Treść alternatywna, w której nie zostało nic poza pustymi gałęziami, znika w całości.
    for node in list(root.iter(_q(MC, 'AlternateContent'))):
        if node.getparent() is not None and all(len(branch) == 0 for branch in node):
            _remove(node)
    return count


def _references(root):
    """Identyfikatory relacji użyte w części (r:id, r:embed, r:link, o:relid…)."""
    used = set()
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.startswith(f'{{{R}}}') or key == _q(OFFICE, 'relid'):
                used.add(value)
    return used


def strip_images(source):
    """(BytesIO bez obrazów, liczba usuniętych obrazów)."""
    original = _read(source)
    with ZipFile(BytesIO(original)) as archive:
        names = archive.namelist()
        payloads = {name: archive.read(name) for name in names if _is_xml(name)}
    parts, count = {}, 0
    roots = {}
    for name, data in payloads.items():
        if name.endswith('.rels') or name == '[Content_Types].xml':
            continue
        if not any(marker in data for marker in IMAGE_MARKERS):
            continue
        try:
            root = etree.fromstring(data, PARSER)
        except etree.XMLSyntaxError:
            continue
        removed = _strip_part(root)
        if removed:
            count += removed
            parts[name] = _serialize(root)
            roots[name] = root
    # Relacje obrazów, do których nic już nie odsyła, i ich pliki.
    dropped_targets = set()
    for rels_name, data in payloads.items():
        if not rels_name.endswith('.rels'):
            continue
        part = _source_part(rels_name)
        if part not in names and rels_name != '_rels/.rels':
            continue
        media = None
        try:
            rels = etree.fromstring(data, PARSER)
        except etree.XMLSyntaxError:
            continue
        for relationship in list(rels):
            kind = relationship.get('Type', '')
            if rels_name == '_rels/.rels' and kind.endswith(THUMBNAIL_RELATIONSHIP) and count:
                target = _target(rels_name, relationship)
                rels.remove(relationship)
                if target:
                    dropped_targets.add(target)
                parts[rels_name] = _serialize(rels)
                continue
            if not kind.endswith(MEDIA_RELATIONSHIPS):
                continue
            if media is None:
                root = roots.get(part)
                if root is None:
                    try:
                        root = etree.fromstring(payloads.get(part, b''), PARSER)
                    except etree.XMLSyntaxError:
                        media = {relationship.get('Id') for relationship in rels}  # nie ruszamy
                        continue
                media = _references(root)
            if relationship.get('Id') in media:
                continue
            target = _target(rels_name, relationship)
            rels.remove(relationship)
            if target:
                dropped_targets.add(target)
            parts[rels_name] = _serialize(rels)
    # Plik usuwamy, gdy żadna pozostała relacja w pakiecie już do niego nie prowadzi.
    still_used = set()
    for rels_name, data in payloads.items():
        if not rels_name.endswith('.rels'):
            continue
        try:
            rels = etree.fromstring(parts.get(rels_name, data), PARSER)
        except etree.XMLSyntaxError:
            continue
        for relationship in rels:
            target = _target(rels_name, relationship)
            if target:
                still_used.add(target)
    removed_parts = {name for name in dropped_targets - still_used if name in names}
    removed_parts |= {_rels_name(name) for name in removed_parts if _rels_name(name) in names}
    if removed_parts:
        types = etree.fromstring(payloads['[Content_Types].xml'], PARSER)
        remaining = [name for name in names if name not in removed_parts]
        used_extensions = {posixpath.splitext(name)[1].lstrip('.').lower() for name in remaining}
        for entry in list(types):
            if entry.tag == _q(CT, 'Override') and entry.get('PartName', '').lstrip('/') in removed_parts:
                types.remove(entry)
            elif entry.tag == _q(CT, 'Default'):
                extension = entry.get('Extension', '').lower()
                if extension in IMAGE_EXTENSIONS and extension not in used_extensions:
                    types.remove(entry)
        parts['[Content_Types].xml'] = _serialize(types)
    if not parts:
        return BytesIO(original), 0
    return _write(original, parts, removed_parts), count


def _polish_rpr(rpr):
    lang = rpr.find(_q(W, 'lang'))
    if lang is None:
        lang = etree.Element(_q(W, 'lang'))
        following = next((child for child in rpr if child.tag in AFTER_LANG), None)
        if following is not None:
            following.addprevious(lang)
        else:
            rpr.append(lang)
    lang.set(_q(W, 'val'), POLISH)


def force_polish(source):
    """Każde oznaczenie języka w:lang (treść, style, ustawienia domyślne, wszystkie części) → pl-PL."""
    original = _read(source)
    parts = {}
    with ZipFile(BytesIO(original)) as archive:
        for name in archive.namelist():
            if not name.startswith('word/') or not name.endswith('.xml'):
                continue
            data = archive.read(name)
            is_styles = name == 'word/styles.xml'
            if b'lang' not in data and b'Lang' not in data and not is_styles:
                continue
            try:
                root = etree.fromstring(data, PARSER)
            except etree.XMLSyntaxError:
                continue
            changed = False
            for node in root.iter(_q(W, 'lang'), _q(W, 'themeFontLang')):
                if node.get(_q(W, 'val')) != POLISH:
                    node.set(_q(W, 'val'), POLISH)
                    changed = True
            if is_styles and root.tag == _q(W, 'styles'):
                # Ustawienia domyślne dokumentu: tekst bez własnego oznaczenia też jest polski.
                defaults = root.find(_q(W, 'docDefaults'))
                if defaults is None:
                    defaults = etree.Element(_q(W, 'docDefaults'))
                    root.insert(0, defaults)
                run_defaults = defaults.find(_q(W, 'rPrDefault'))
                if run_defaults is None:
                    run_defaults = etree.Element(_q(W, 'rPrDefault'))
                    defaults.insert(0, run_defaults)
                properties = run_defaults.find(_q(W, 'rPr'))
                if properties is None:
                    properties = etree.SubElement(run_defaults, _q(W, 'rPr'))
                lang = properties.find(_q(W, 'lang'))
                if lang is None or lang.get(_q(W, 'val')) != POLISH:
                    _polish_rpr(properties)
                    changed = True
            if changed:
                parts[name] = _serialize(root)
    if not parts:
        return BytesIO(original)
    return _write(original, parts)
