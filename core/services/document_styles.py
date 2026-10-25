"""Resolve inherited DOCX properties, including malformed cyclic style trees.

Tu jest też jedyna reguła nagłówków i punktów listy (ParagraphOutline), z której korzystają wszystkie programy
dokumentów: miękkie entery, ujednolicanie formatowania, przebudowa, podział, Odkurzacz i e-book/PDF.
"""
import re

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn

if __package__:
    from .document_xml import w
else:
    from document_xml import w

# Wyrównanie z OOXML Strict (LibreOffice „Office Open XML Text”, Google Docs), którego python-docx nie zna.
ALIGNMENT_ALIASES = {'start': WD_ALIGN_PARAGRAPH.LEFT, 'end': WD_ALIGN_PARAGRAPH.RIGHT}

# Nazwa albo id stylu nagłówka z numerem: „heading 1” (Word), „Nagłówek 1” (polskie nazwy, np. z LibreOffice),
# „Naglowek 1”, id Heading1, Nagwek1 (polski Word), Naglowek1 – bez względu na wielkość liter i spację.
HEADING_STYLE_NAME = re.compile(r'(?:heading|nagłówek|naglowek|nagwek)\s*([1-9])', re.IGNORECASE)
# w:outlineLvl 0–8 to poziomy 1–9 panelu nawigacji; 9 („tekst podstawowy”) i wartości błędne – brak nagłówka.
OUTLINE_LEVEL_VALUES = frozenset('012345678')
STYLE_SWITCH_ON = ('1', 'true', 'on')


def _heading_number(text):
    match = HEADING_STYLE_NAME.fullmatch((text or '').strip())
    return int(match[1]) if match else None


def _outline_level(node):
    value = node.get(w('val'))
    return int(value) + 1 if value in OUTLINE_LEVEL_VALUES else None


class ParagraphOutline:
    """Poziom nagłówka (1–9 albo None) i punkt listy akapitu – jedna reguła wszystkich programów dokumentów.

    Nagłówek poziomu N to to, co Word pokazuje w panelu nawigacji jako poziom N: rozstrzyga w:outlineLvl akapitu,
    a bez niego pierwszy styl z łańcucha stylu akapitu (w:basedOn w górę), który ma w:outlineLvl albo nazwę lub id
    nagłówka z numerem (HEADING_STYLE_NAME – zapas dla plików bez poziomu konspektu). w:outlineLvl 9 („tekst
    podstawowy”) znosi nagłówek ze stylu. Numeracja nie wpływa na nagłówek. Punkt listy: w:numPr/w:numId akapitu,
    a bez niego pierwszy w łańcuchu stylów; numId 0 wyłącza numerację.

    Akapit bez w:pStyle ma styl domyślny; styl bez definicji (np. plik bez części stylów) jest nagłówkiem tylko
    wtedy, gdy jego id pasuje do wzorca. Działa na elementach lxml (w python-docx: paragraph._p i
    document.styles.element); wynik dla stylu jest liczony raz.
    """

    def __init__(self, styles=None):
        """styles – korzeń styles.xml (None: dokument bez części stylów)."""
        self.styles, self.default = {}, None
        for style in styles.iter(w('style')) if styles is not None else ():
            style_id = style.get(w('styleId'))
            if style_id is None or style.get(w('type'), 'paragraph') != 'paragraph':
                continue
            self.styles.setdefault(style_id, style)
            if style.get(w('default')) in STYLE_SWITCH_ON:
                self.default = style_id  # jak Word i python-docx: ostatni styl domyślny
        self._levels, self._numbering = {}, {}

    def _chain(self, style_id):
        """(id, element albo None dla stylu bez definicji) od stylu akapitu w górę w:basedOn, bez pętli."""
        seen, current = set(), style_id or self.default
        while current is not None and current not in seen:
            seen.add(current)
            style = self.styles.get(current)
            yield current, style
            based = style.find(w('basedOn')) if style is not None else None
            current = based.get(w('val')) if based is not None else None

    def style_level(self, style_id):
        """Poziom nagłówka akapitu w stylu style_id (None – styl domyślny) bez własnego w:outlineLvl."""
        if style_id not in self._levels:
            level = None
            for current, style in self._chain(style_id):
                if style is None:
                    level = _heading_number(current)
                    break
                node = style.find(f'{w("pPr")}/{w("outlineLvl")}')
                if node is not None:
                    level = _outline_level(node)
                    break
                name = style.find(w('name'))
                level = _heading_number(name.get(w('val')) if name is not None else None) or _heading_number(current)
                if level:
                    break
            self._levels[style_id] = level
        return self._levels[style_id]

    def heading_level(self, paragraph):
        """Poziom nagłówka akapitu w:p (1–9) albo None."""
        properties = paragraph.find(w('pPr'))
        node = properties.find(w('outlineLvl')) if properties is not None else None
        if node is not None:
            return _outline_level(node)
        return self.style_level(_paragraph_style_id(properties))

    def _style_numbering(self, style_id):
        """Pierwszy w:numId w łańcuchu stylów albo None."""
        if style_id not in self._numbering:
            self._numbering[style_id] = None
            for _, style in self._chain(style_id):
                node = style.find(f'{w("pPr")}/{w("numPr")}/{w("numId")}') if style is not None else None
                if node is not None:
                    self._numbering[style_id] = node
                    break
        return self._numbering[style_id]

    def is_list_item(self, paragraph):
        """Czy akapit w:p jest punktem listy (numeracja z akapitu albo ze stylu, numId 0 – wyłączona)."""
        properties = paragraph.find(w('pPr'))
        node = properties.find(f'{w("numPr")}/{w("numId")}') if properties is not None else None
        if node is None:
            node = self._style_numbering(_paragraph_style_id(properties))
        return node is not None and node.get(w('val')) not in (None, '0')


def _paragraph_style_id(properties):
    node = properties.find(w('pStyle')) if properties is not None else None
    return (node.get(w('val')) or None) if node is not None else None


def document_outline(document):
    """ParagraphOutline dokumentu python-docx. Brakującej części stylów nie tworzy (document.styles dopisałby
    do pliku domyślną)."""
    try:
        part = document.part.part_related_by(RT.STYLES)
    except KeyError:
        return ParagraphOutline()
    return ParagraphOutline(part.element)


def unknown_format_value(formatting, name):
    """Wartość zastępcza właściwości, której zapisu python-docx nie zna (ValueError przy odczycie).

    Wyrównanie „start”/„end” jest mapowane na lewe/prawe; każda inna nieznana wartość jest nieokreślona
    (None), więc decyduje styl albo ustawienie domyślne – zamiast wyjątku przerywającego program.
    """
    if name == 'alignment':
        properties = getattr(formatting._element, 'pPr', None)
        jc = properties.jc if properties is not None else None
        if jc is not None:
            return ALIGNMENT_ALIASES.get(jc.get(qn('w:val')))
    return None


def read_property(formatting, name):
    """Właściwość formatowania akapitu (python-docx) z tolerancją na nieznane wartości."""
    try:
        return getattr(formatting, name)
    except ValueError:
        return unknown_format_value(formatting, name)


DROP_CAP_RUN_PROPERTIES = ('w:sz', 'w:szCs', 'w:position')


def merge_drop_caps(root):
    """Inicjały Worda (akapit z w:framePr w:dropCap) wracają na początek następnego akapitu; zwraca ich liczbę.

    Word zapisuje inicjał jako osobny akapit z pierwszą literą w ramce, a resztę słowa w akapicie następnym –
    bez scalenia e-book, PDF i przebudowa pokazałyby „W” i „ielki…” jako dwa akapity. Litera traci rozmiar
    i przesunięcie inicjału (dostaje formatowanie akapitu). Inicjał bez akapitu po nim zostaje bez zmian.
    """
    count = 0
    for paragraph in list(root.iter(qn('w:p'))):
        frame = paragraph.find(f'{qn("w:pPr")}/{qn("w:framePr")}')
        following = paragraph.getnext()
        if (frame is None or frame.get(qn('w:dropCap')) not in ('drop', 'margin')
                or following is None or following.tag != qn('w:p')):
            continue
        properties = following.find(qn('w:pPr'))
        index = 0 if properties is None else following.index(properties) + 1
        for child in list(paragraph):
            if child.tag == qn('w:pPr'):
                continue
            for run_properties in child.iter(qn('w:rPr')):
                for node in [node for name in DROP_CAP_RUN_PROPERTIES for node in run_properties.findall(qn(name))]:
                    run_properties.remove(node)
            following.insert(index, child)
            index += 1
        paragraph.getparent().remove(paragraph)
        count += 1
    return count


def style_chain(style):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        yield style
        style = style.base_style


def paragraph_style(paragraph):
    """paragraph.style z pamięcią podręczną części dokumentu.

    python-docx przy każdym odczycie przeszukuje wszystkie style (także styl
    domyślny), co w długich dokumentach kosztowało sekundy. Wynik zależy tylko
    od identyfikatora stylu akapitu, a style nie zmieniają się w trakcie
    konwersji.
    """
    part = paragraph.part
    cache = part.__dict__.setdefault('_paragraph_style_cache', {})
    key = paragraph._p.style
    if key not in cache:
        cache[key] = paragraph.style
    return cache[key]


def paragraph_properties(paragraph, names):
    """Resolve several properties with one lazy traversal of the style tree."""
    formatting = paragraph.paragraph_format
    values = {name: read_property(formatting, name) for name in names}
    missing = {name for name, value in values.items() if value is None}
    if missing:
        for style in style_chain(paragraph_style(paragraph)):
            formatting = style.paragraph_format
            for name in tuple(missing):
                value = read_property(formatting, name)
                if value is not None:
                    values[name] = value
                    missing.remove(name)
            if not missing:
                break
    return values
