"""Obrazy i język dokumentu przed wszelkimi zmianami (wszystkie programy).

unlink_external_images – usuwa połączenia obrazów z plikami spoza dokumentu (relacje
TargetMode="External"): obraz osadzony i połączony zostaje bez linku, obraz tylko
połączony znika. CMS nigdy nie pobiera takich plików.
strip_images – usuwa obrazy z każdej części dokumentu (treść, nagłówki, stopki,
przypisy, komentarze, pola tekstowe, numeracja z obrazkowymi punktorami) razem
z nieużywanymi plikami mediów, ich relacjami i wpisami [Content_Types].xml.
strip_embedded_fonts – usuwa czcionki osadzone przez Worda (pliki czcionek, odwołania
w fontTable i ustawienie osadzania); opcja Odkurzacza i zawsze przy imporcie ze skrzynki.
force_polish – oznacza cały dokument jako polski (każde w:lang w:val = pl-PL,
także w stylach i ustawieniach domyślnych).

Moduł działa w izolowanym podprocesie konwertera: tylko biblioteka standardowa i lxml.
"""
import posixpath
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

if __package__:
    from .document_xml import MC, R, W, is_internal, parse_part, part_target, rels_path, serialize_part, source_part
else:
    from document_xml import MC, R, W, is_internal, parse_part, part_target, rels_path, serialize_part, source_part

POLISH = 'pl-PL'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PIC = 'http://schemas.openxmlformats.org/drawingml/2006/picture'
V = 'urn:schemas-microsoft-com:vml'
OFFICE = 'urn:schemas-microsoft-com:office:office'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'

# Relacje prowadzące do obrazów i osadzonych obiektów: po usunięciu odwołań w treści znikają.
MEDIA_RELATIONSHIPS = ('/image', '/oleObject', '/package', '/hdphoto', '/media', '/video', '/audio')
# Miniatura pakietu (podgląd pierwszej strony w eksploratorze plików) pokazywałaby usunięte obrazy.
THUMBNAIL_RELATIONSHIP = '/metadata/thumbnail'
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'jpe', 'gif', 'bmp', 'dib', 'tif', 'tiff', 'emf', 'wmf', 'emz', 'wmz',
                    'svg', 'webp', 'pict', 'jfif', 'wdp', 'ico'}
# Skrót, czy część w ogóle może zawierać obraz (bez parsowania dużych plików XML).
IMAGE_MARKERS = (b'drawing', b'pict', b'object', b'imagedata', b'blip', b'background', b'PicBullet', b'fill')
# Część XML w UTF-16 (znacznik BOM) skrót bajtowy by pominął – taką część zawsze parsujemy.
UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')
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
        if not data.startswith(UTF16_BOMS) and not any(marker in data for marker in IMAGE_MARKERS):
            continue
        try:
            root = parse_part(data)
        except etree.XMLSyntaxError:
            continue
        removed = _strip_part(root)
        if removed:
            count += removed
            parts[name] = serialize_part(root)
            roots[name] = root
    # Relacje obrazów, do których nic już nie odsyła, i ich pliki.
    dropped_targets = set()
    for rels_name, data in payloads.items():
        if not rels_name.endswith('.rels'):
            continue
        part = source_part(rels_name)
        if part not in names and rels_name != '_rels/.rels':
            continue
        media = None
        try:
            rels = parse_part(data)
        except etree.XMLSyntaxError:
            continue
        for relationship in list(rels):
            kind = relationship.get('Type', '')
            if rels_name == '_rels/.rels' and kind.endswith(THUMBNAIL_RELATIONSHIP) and count:
                target = part_target(part, relationship)
                rels.remove(relationship)
                if target:
                    dropped_targets.add(target)
                parts[rels_name] = serialize_part(rels)
                continue
            if not kind.endswith(MEDIA_RELATIONSHIPS):
                continue
            if media is None:
                root = roots.get(part)
                if root is None:
                    try:
                        root = parse_part(payloads.get(part, b''))
                    except etree.XMLSyntaxError:
                        media = {relationship.get('Id') for relationship in rels}  # nie ruszamy
                        continue
                media = _references(root)
            if relationship.get('Id') in media:
                continue
            target = part_target(part, relationship)
            rels.remove(relationship)
            if target:
                dropped_targets.add(target)
            parts[rels_name] = serialize_part(rels)
    # Plik usuwamy, gdy żadna pozostała relacja w pakiecie już do niego nie prowadzi.
    still_used = set()
    for rels_name, data in payloads.items():
        if not rels_name.endswith('.rels'):
            continue
        try:
            rels = parse_part(parts.get(rels_name, data))
        except etree.XMLSyntaxError:
            continue
        for relationship in rels:
            target = part_target(source_part(rels_name), relationship)
            if target:
                still_used.add(target)
    removed_parts = {name for name in dropped_targets - still_used if name in names}
    removed_parts |= {rels_path(name) for name in removed_parts if rels_path(name) in names}
    if removed_parts:
        types = parse_part(payloads['[Content_Types].xml'])
        remaining = [name for name in names if name not in removed_parts]
        used_extensions = {posixpath.splitext(name)[1].lstrip('.').lower() for name in remaining}
        for entry in list(types):
            if entry.tag == _q(CT, 'Override') and entry.get('PartName', '').lstrip('/') in removed_parts:
                types.remove(entry)
            elif entry.tag == _q(CT, 'Default'):
                extension = entry.get('Extension', '').lower()
                if extension in IMAGE_EXTENSIONS and extension not in used_extensions:
                    types.remove(entry)
        parts['[Content_Types].xml'] = serialize_part(types)
    if not parts:
        return BytesIO(original), 0
    return _write(original, parts, removed_parts), count


# Osadzone czcionki (Word: Opcje → Zapisywanie → „Osadź czcionki w pliku”): pliki czcionek z relacji /font
# części fontTable, odwołania do nich i ustawienia, które każą Wordowi osadzać czcionki przy kolejnym zapisie.
FONT_RELATIONSHIP = '/font'
FONT_EMBEDS = {_q(W, name) for name in ('embedRegular', 'embedBold', 'embedItalic', 'embedBoldItalic')}
FONT_SETTINGS = {_q(W, name) for name in ('embedTrueTypeFonts', 'embedSystemFonts', 'saveSubsetFonts')}
FONT_EXTENSIONS = {'odttf', 'ttf', 'otf', 'fntdata'}


def strip_embedded_fonts(source):
    """(BytesIO bez osadzonych czcionek, liczba usuniętych bajtów plików czcionek).

    Nazwy czcionek w tekście i w fontTable zostają – Word użyje czcionek z systemu.
    Dokument bez osadzonych czcionek wraca bez zmian.
    """
    original = _read(source)
    with ZipFile(BytesIO(original)) as archive:
        sizes = {item.filename: item.compress_size for item in archive.infolist()}
        payloads = {name: archive.read(name) for name in sizes
                    if name.endswith('.rels') or name == '[Content_Types].xml'
                    or name.endswith(('fontTable.xml', 'settings.xml'))}
    parts, fonts = {}, set()
    for rels_name, data in payloads.items():
        if not rels_name.endswith('.rels') or b'/font"' not in data:
            continue
        rels = parse_part(data)
        part = source_part(rels_name)
        for relationship in list(rels):
            if relationship.get('Type', '').endswith(FONT_RELATIONSHIP) and is_internal(relationship):
                target = part_target(part, relationship)
                if target in sizes:
                    fonts.add(target)
                rels.remove(relationship)
        parts[rels_name] = serialize_part(rels)
        if not len(rels):
            fonts.add(rels_name)
    for name, data in payloads.items():
        tags = FONT_EMBEDS if name.endswith('fontTable.xml') else FONT_SETTINGS if name.endswith('settings.xml') else ()
        if not tags:
            continue
        root = parse_part(data)
        found = [node for node in root.iter() if node.tag in tags]
        for node in found:
            _remove(node)
        if found:
            parts[name] = serialize_part(root)
    if not fonts and not parts:
        return BytesIO(original), 0
    types = parse_part(payloads['[Content_Types].xml'])
    remaining = {posixpath.splitext(name)[1].lstrip('.').lower() for name in sizes if name not in fonts}
    for entry in list(types):
        if entry.tag == _q(CT, 'Override') and entry.get('PartName', '').lstrip('/') in fonts:
            types.remove(entry)
        elif entry.tag == _q(CT, 'Default') and entry.get('Extension', '').lower() in FONT_EXTENSIONS - remaining:
            types.remove(entry)
    parts['[Content_Types].xml'] = serialize_part(types)
    removed = sum(sizes[name] for name in fonts if not name.endswith('.rels'))
    return _write(original, parts, fonts), removed


def embedded_fonts_note(removed):
    """Uwaga dla użytkownika o usuniętych osadzonych czcionkach ('' – nie było ich)."""
    if removed <= 0:
        return ''
    size = f'{removed / (1024 * 1024):.1f} MB' if removed >= 100 * 1024 else f'{max(1, round(removed / 1024))} KB'
    return (f'Usunięto osadzone czcionki ({size.replace(".", ",")}) – Word użyje czcionek '
            'zainstalowanych w systemie.')


# Obraz połączony (Wstaw → Obrazy → Połącz z plikiem; pole INCLUDEPICTURE): relacja obrazu z TargetMode="External".
LINKED_IMAGE_RELATIONSHIPS = ('/image', '/hdphoto')
SVG_IMAGES = 'http://schemas.microsoft.com/office/drawing/2016/SVG/main'
# Atrybuty, którymi rysunek lub kształt VML wskazuje plik obrazu.
IMAGE_SOURCE_ATTRIBUTES = (_q(R, 'embed'), _q(R, 'link'), _q(R, 'id'), _q(R, 'pict'), _q(OFFICE, 'relid'))


def is_linked_image(rels_name, relationship):
    """Czy relacja (element Relationship z pliku `rels_name`) to obraz połączony z plikiem spoza pakietu
    – także zewnętrzna miniatura pakietu. Takie relacje usuwa unlink_external_images."""
    if is_internal(relationship):
        return False
    kind = relationship.get('Type', '')
    return kind.endswith(LINKED_IMAGE_RELATIONSHIPS) or (
        rels_name == '_rels/.rels' and kind.endswith(THUMBNAIL_RELATIONSHIP))


def _remove_picture(node):
    """Usuwa obraz, któremu po usunięciu linku nie zostało źródło; True, gdy coś usunięto.

    Rysunek z samym obrazem znika w całości, obraz w grupie – sam, wypełnienie kształtu obrazem
    staje się brakiem wypełnienia, kształt VML i obiekt OLE znikają (pole tekstowe VML traci tylko
    obraz), obrazkowy punktor listy – razem z odwołaniami poziomów list.
    """
    bullet = next(node.iterancestors(_q(W, 'numPicBullet')), None)
    if bullet is not None:
        identifier = bullet.get(_q(W, 'numPicBulletId'))
        for reference in list(node.getroottree().getroot().iter(_q(W, 'lvlPicBulletId'))):
            if reference.get(_q(W, 'val')) == identifier:
                _remove(reference)
        _remove(bullet)
        return True
    picture = next(node.iterancestors(_q(PIC, 'pic')), None)
    if picture is not None:
        drawing = next(picture.iterancestors(_q(W, 'drawing')), None)
        data = picture.getparent()
        whole = drawing is not None and data is not None and data.get('uri', '').endswith('/picture')
        _remove(drawing if whole else picture)
        return True
    parent = node.getparent()
    if node.tag == _q(A, 'blip'):
        if parent is None or parent.tag != _q(A, 'blipFill'):
            return False  # inne użycie (np. punktor DrawingML): zostaje bez źródła
        parent.addnext(etree.Element(_q(A, 'noFill')))
        _remove(parent)
        return True
    if node.tag == _q(V, 'imagedata'):
        holder = next(node.iterancestors(_q(W, 'object'), _q(W, 'pict')), None)
        if holder is not None and (holder.tag == _q(W, 'object') or not any(
                True for _ in holder.iter(_q(V, 'textbox'), _q(W, 'txbxContent')))):
            _remove(holder)
            return True
    _remove(node)
    return True


def _unlink_part(root, external):
    """Usuwa z części odwołania do relacji `external`; zwraca liczbę usuniętych obrazów tylko połączonych."""
    count = 0
    for node in list(root.iter()):
        if not isinstance(node.tag, str) or node.getroottree().getroot() is not root:
            continue  # komentarz XML albo element usunięty razem z rysunkiem
        linked = [key for key, value in node.attrib.items()
                  if (key.startswith(f'{{{R}}}') or key == _q(OFFICE, 'relid')) and value in external]
        if not linked:
            continue
        for key in linked:
            del node.attrib[key]
        if node.tag == _q(V, 'imagedata'):
            node.attrib.pop(_q(OFFICE, 'href'), None)  # zapisana ścieżka połączonego pliku
        if node.tag in (_q(A, 'blip'), _q(V, 'imagedata'), _q(V, 'fill')):
            if not any(node.get(key) for key in IMAGE_SOURCE_ATTRIBUTES):
                counted = _counted(node)
                count += _remove_picture(node) and counted
        elif node.tag == _q(SVG_IMAGES, 'svgBlip') and not node.get(_q(R, 'embed')):
            # Wersja SVG obrazu bez pliku: zostaje obraz zastępczy z a:blip.
            extension = node.getparent()
            if extension is not None and extension.tag == _q(A, 'ext'):
                extensions = extension.getparent()
                _remove(extension)
                if extensions is not None and len(extensions) == 0:
                    _remove(extensions)
    for node in list(root.iter(_q(MC, 'AlternateContent'))):
        if node.getparent() is not None and all(len(branch) == 0 for branch in node):
            _remove(node)
    return count


def unlink_external_images(source):
    """(BytesIO bez połączeń obrazów z plikami spoza dokumentu, liczba usuniętych obrazów tylko połączonych).

    Decyzja właściciela: obraz połączony (relacja TargetMode="External") nie blokuje pliku – usuwamy
    sam link. Obraz osadzony i połączony (r:embed i r:link) zostaje osadzony, bez linku; obraz tylko
    połączony znika, bo bez linku nie miałby źródła. Relacje znikają z pakietu, więc żaden program
    (ani biblioteka konwertera) ich nie odczyta. Hiperłącza zewnętrzne to nie obrazy – zostają.
    """
    original = _read(source)
    parts, count = {}, 0
    with ZipFile(BytesIO(original)) as archive:
        names = set(archive.namelist())
        for rels_name in archive.namelist():
            if not rels_name.endswith('.rels'):
                continue
            data = archive.read(rels_name)
            if b'external' not in data.lower() and not data.startswith(UTF16_BOMS):
                continue
            try:
                rels = parse_part(data)
            except etree.XMLSyntaxError:
                continue
            linked = [relationship for relationship in rels if is_linked_image(rels_name, relationship)]
            if not linked:
                continue
            for relationship in linked:
                rels.remove(relationship)
            parts[rels_name] = serialize_part(rels)
            part = source_part(rels_name)
            if part in names and _is_xml(part):
                try:
                    root = parse_part(archive.read(part))
                except etree.XMLSyntaxError:
                    continue
                count += _unlink_part(root, {relationship.get('Id') for relationship in linked})
                parts[part] = serialize_part(root)
    if not parts:
        return BytesIO(original), 0
    return _write(original, parts), count


def linked_images_note(count):
    """Uwaga dla użytkownika o usuniętych obrazach połączonych z plikami spoza dokumentu ('' – nie było ich)."""
    if count <= 0:
        return ''
    last, two = count % 10, count % 100
    if count == 1:
        words = 'obraz połączony z plikiem zewnętrznym'
    elif 2 <= last <= 4 and not 12 <= two <= 14:
        words = 'obrazy połączone z plikami zewnętrznymi'
    else:
        words = 'obrazów połączonych z plikami zewnętrznymi'
    return f'Usunięto {count} {words} – pliki spoza dokumentu nie są pobierane.'


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
            if b'lang' not in data and b'Lang' not in data and not is_styles and not data.startswith(UTF16_BOMS):
                continue
            try:
                root = parse_part(data)
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
                parts[name] = serialize_part(root)
    if not parts:
        return BytesIO(original)
    return _write(original, parts)
