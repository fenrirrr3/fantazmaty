"""Resolve inherited DOCX properties, including malformed cyclic style trees."""

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
    value = getattr(paragraph.paragraph_format, name)
    if value is not None:
        return value
    for style in style_chain(paragraph_style(paragraph)):
        value = getattr(style.paragraph_format, name)
        if value is not None:
            return value
    return None


def paragraph_properties(paragraph, names):
    """Resolve several properties with one lazy traversal of the style tree."""
    formatting = paragraph.paragraph_format
    values = {name: getattr(formatting, name) for name in names}
    missing = {name for name, value in values.items() if value is None}
    if missing:
        for style in style_chain(paragraph_style(paragraph)):
            formatting = style.paragraph_format
            for name in tuple(missing):
                value = getattr(formatting, name)
                if value is not None:
                    values[name] = value
                    missing.remove(name)
            if not missing:
                break
    return values
