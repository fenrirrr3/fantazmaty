"""Resolve inherited DOCX properties, including malformed cyclic style trees."""

def style_chain(style):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        yield style
        style = style.base_style


def paragraph_property(paragraph, name):
    value = getattr(paragraph.paragraph_format, name)
    if value is not None:
        return value
    for style in style_chain(paragraph.style):
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
        for style in style_chain(paragraph.style):
            formatting = style.paragraph_format
            for name in tuple(missing):
                value = getattr(formatting, name)
                if value is not None:
                    values[name] = value
                    missing.remove(name)
            if not missing:
                break
    return values
