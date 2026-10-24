"""Resolve inherited DOCX properties, including malformed cyclic style trees."""
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

# Wyrównanie z OOXML Strict (LibreOffice „Office Open XML Text”, Google Docs), którego python-docx nie zna.
ALIGNMENT_ALIASES = {'start': WD_ALIGN_PARAGRAPH.LEFT, 'end': WD_ALIGN_PARAGRAPH.RIGHT}


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


def paragraph_property(paragraph, name):
    value = read_property(paragraph.paragraph_format, name)
    if value is not None:
        return value
    for style in style_chain(paragraph_style(paragraph)):
        value = read_property(style.paragraph_format, name)
        if value is not None:
            return value
    return None


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
