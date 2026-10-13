"""Style szablonu wydawnictwa (TEKST Szablon 10-2025.dotx) w scalonym DOCX (Programy → Scalanie DOCX).

Z szablonu przenosimy wyłącznie definicje stylów: cały word/styles.xml (z docDefaults
i latentStyles) oraz wpisy fontTable.xml czcionek, których te style używają (same metadane).
Treść, ustawienia strony, nagłówki i stopki, settings.xml (np. w:attachedTemplate), motyw,
numeracja i właściwości dokumentu zostają z wyniku – wynik pozostaje zwykłym .docx.
Style szablonu nie odwołują się do numeracji (w:numPr) ani do motywu (czcionki i kolory
motywu) – pilnują tego testy; gdyby nowy szablon ich używał, trzeba rozszerzyć ten moduł.

Odwołania do stylów w treści (w:pStyle, w:rStyle, w:tblStyle) przemapowujemy po nazwie
stylu (bez wielkości liter, ten sam typ), np. „heading 1” → Nagwek1. Styl, którego szablon
nie ma, dopisujemy z dotychczasowej definicji (z łańcuchem w:basedOn), z unikalnym id.
"""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / 'resources' / 'TEKST Szablon 10-2025.dotx'
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_blank_text=False)
STYLES = 'word/styles.xml'
FONTS = 'word/fontTable.xml'
# Kopia stylów dla Worda 2007 – po podmianie stylów byłaby z nimi sprzeczna, więc ją usuwamy.
STYLES_WITH_EFFECTS = 'word/stylesWithEffects.xml'
CONTENT_TYPES = '[Content_Types].xml'
DOCUMENT_RELATIONS = 'word/_rels/document.xml.rels'
# Odwołanie do stylu → typ stylu, do którego może prowadzić.
REFERENCES = {'pStyle': 'paragraph', 'rStyle': 'character', 'tblStyle': 'table'}
FONT_ATTRIBUTES = ('ascii', 'hAnsi', 'eastAsia', 'cs')


def w(name):
    return f'{{{W}}}{name}'


def _xml(root):
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def _styles(root):
    """id → element stylu oraz (typ, nazwa małymi literami) → id."""
    by_id, by_name = {}, {}
    for style in root.findall(w('style')):
        style_id = style.get(w('styleId'))
        name = style.find(w('name'))
        by_id[style_id] = style
        if name is not None:
            by_name.setdefault((style.get(w('type'), 'paragraph'), name.get(w('val'), '').lower()), style_id)
    return by_id, by_name


class _StyleMap:
    """Przemapowanie id stylów wyniku na style szablonu; brakujące style dopisuje do szablonu."""

    def __init__(self, source, template):
        self.source, _ = _styles(source)
        self.template = template
        self.ids, self.names = _styles(template)
        self.mapping = {}
        self.copied = []

    def resolve(self, style_id, kind):
        """Id stylu w wyniku po zmianie; None, gdy styl nie istnieje ani w wyniku, ani w szablonie."""
        if style_id in self.mapping:
            return self.mapping[style_id]
        style = self.source.get(style_id)
        if style is None:
            # Odwołanie bez definicji: zostawiamy, jeśli szablon ma taki id (Word i tak go szuka po id).
            return style_id if style_id in self.ids else None
        match = self._match(style, kind)
        if match is not None:
            self.mapping[style_id] = match
            return match
        return self._copy(style_id, style)

    def _match(self, style, kind):
        """Id stylu szablonu o tej samej nazwie i typie (bez wielkości liter) albo None."""
        name = style.find(w('name'))
        if name is None:
            return None
        return self.names.get((style.get(w('type'), kind), name.get(w('val'), '').lower()))

    def _copy(self, style_id, style):
        new_id, suffix = style_id, 1
        while new_id in self.ids:
            suffix += 1
            new_id = f'{style_id}{suffix}'
        copy = deepcopy(style)
        copy.set(w('styleId'), new_id)
        copy.attrib.pop(w('default'), None)  # domyślne style ma już szablon
        self.mapping[style_id] = new_id
        self.ids[new_id] = copy
        self.copied.append(copy)
        based = copy.find(w('basedOn'))
        if based is not None:
            target = self.resolve(based.get(w('val')), copy.get(w('type'), 'paragraph'))
            if target is None:
                copy.remove(based)
            else:
                based.set(w('val'), target)
        return new_id

    def finish(self):
        """Dopisuje skopiowane style; w:next i w:link tylko do stylów, które istnieją w wyniku."""
        for copy in self.copied:
            for name in ('next', 'link'):
                for node in copy.findall(w(name)):
                    value = node.get(w('val'))
                    target = self.mapping.get(value)
                    if target is None and value in self.source:
                        target = self._match(self.source[value], copy.get(w('type'), 'paragraph'))
                    if target is None:
                        copy.remove(node)
                    else:
                        node.set(w('val'), target)
            self.template.append(copy)


def _remap(root, styles):
    for node in list(root.iter(*(w(name) for name in REFERENCES))):
        target = styles.resolve(node.get(w('val')), REFERENCES[etree.QName(node).localname])
        if target is None:
            node.getparent().remove(node)
        else:
            node.set(w('val'), target)


def _font_names(styles_root):
    return {value for node in styles_root.iter(w('rFonts'))
            for value in (node.get(w(name)) for name in FONT_ATTRIBUTES) if value}


def _fonts(result, template, used):
    """Wpisy fontTable szablonu dla czcionek stylów, których wynik jeszcze nie opisuje."""
    known = {font.get(w('name')) for font in result.findall(w('font'))}
    for font in template.findall(w('font')):
        if font.get(w('name')) in used and font.get(w('name')) not in known:
            result.append(deepcopy(font))


def _without_styles_with_effects(data):
    """[Content_Types].xml albo relacje dokumentu bez wpisu stylesWithEffects.xml."""
    root = etree.fromstring(data, PARSER)
    for node in list(root):
        if (node.get('PartName') == '/' + STYLES_WITH_EFFECTS
                or node.get('Target') == STYLES_WITH_EFFECTS.removeprefix('word/')):
            root.remove(node)
    return _xml(root)


def apply_template_styles(payload, template_path=TEMPLATE_PATH):
    """DOCX (bajty) ze stylami szablonu w miejsce dotychczasowych; treść i formatowanie bez zmian."""
    with ZipFile(template_path) as package:
        template_styles = etree.fromstring(package.read(STYLES), PARSER)
        template_fonts = etree.fromstring(package.read(FONTS), PARSER)
    with ZipFile(BytesIO(payload)) as source:
        names = source.namelist()
        styles = _StyleMap(etree.fromstring(source.read(STYLES), PARSER), template_styles)
        parts = {}
        # Odwołania do stylów: treść i inne części (np. numeracja wiąże poziomy list ze stylami).
        for name in names:
            if (name.startswith('word/') and name.endswith('.xml') and '/' not in name[5:]
                    and name not in (STYLES, STYLES_WITH_EFFECTS, FONTS)):
                root = etree.fromstring(source.read(name), PARSER)
                if any(True for _ in root.iter(*(w(item) for item in REFERENCES))):
                    _remap(root, styles)
                    parts[name] = _xml(root)
        styles.finish()
        parts[STYLES] = _xml(template_styles)
        if FONTS in names:
            fonts = etree.fromstring(source.read(FONTS), PARSER)
            _fonts(fonts, template_fonts, _font_names(template_styles))
            parts[FONTS] = _xml(fonts)
        if STYLES_WITH_EFFECTS in names:
            for name in (CONTENT_TYPES, DOCUMENT_RELATIONS):
                parts[name] = _without_styles_with_effects(source.read(name))
        output = BytesIO()
        with ZipFile(output, 'w', ZIP_DEFLATED) as target:
            for item in source.infolist():
                if item.filename == STYLES_WITH_EFFECTS:
                    continue
                target.writestr(item, parts.get(item.filename, source.read(item.filename)), compress_type=ZIP_DEFLATED)
    return output.getvalue()
