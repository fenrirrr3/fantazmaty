"""Resolve inherited DOCX properties, including malformed cyclic style trees."""

def style_chain(style):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        yield style
        style = style.base_style


def paragraph_property(paragraph, name):
    for formatting in (paragraph.paragraph_format, *(s.paragraph_format for s in style_chain(paragraph.style))):
        value = getattr(formatting, name)
        if value is not None:
            return value
    return None
