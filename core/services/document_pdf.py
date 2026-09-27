"""Simple paginated PDF with DOCX page and paragraph settings, bundled fonts."""
from base64 import b64encode
from collections import defaultdict, deque
from pathlib import Path


def inherited(paragraph, attribute):
    value = getattr(paragraph.paragraph_format, attribute)
    style = paragraph.style
    while value is None and style is not None:
        value = getattr(style.paragraph_format, attribute)
        style = style.base_style
    return value


def render_pdf(source, target, content, assets, title):
    from docx import Document
    from fpdf import FPDF
    from fpdf.fonts import TextStyle
    from fpdf.html import HTML2FPDF
    from lxml import html
    document = Document(source)
    section = document.sections[0]
    width, height = section.page_width.mm, section.page_height.mm
    if not (50 <= width <= 600 and 50 <= height <= 600):
        raise ValueError('Unsupported page dimensions')
    margins = [section.left_margin.mm, section.top_margin.mm,
               section.right_margin.mm, section.bottom_margin.mm]
    if min(margins) < 0 or margins[0] + margins[2] >= width - 20 or margins[1] + margins[3] >= height - 20:
        raise ValueError('Unsupported page margins')
    class WordHTML(HTML2FPDF):
        def handle_starttag(self, tag, attrs):
            super().handle_starttag(tag, attrs)
            values = dict(attrs)
            if tag in ('p','h1','h2','h3','h4','h5','h6') and self._paragraph is not None:
                self._paragraph.first_line_indent = float(values.get('data-indent', 0))
                self._paragraph.top_margin = float(values.get('data-before', 0))
                self._paragraph.line_height = float(values.get('line-height', 1.5))

    pdf = FPDF(format=(width, height))
    pdf.HTML2FPDF_CLASS = WordHTML
    font_dir = Path(__file__).with_name('fonts')
    for style, suffix in (('', 'Regular'), ('B', 'Bold'), ('I', 'Italic'), ('BI', 'BoldItalic')):
        pdf.add_font('Document', style=style, fname=font_dir / ('NimbusRoman-' + suffix + '.otf'))
    pdf.set_title(title)
    pdf.set_margins(*margins[:3]); pdf.set_auto_page_break(True, margins[3])
    pdf.add_page(); pdf.set_font('Document', size=12)
    paragraphs = defaultdict(deque)
    normalize = lambda value: ' '.join(value.split())
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            paragraphs[normalize(paragraph.text)].append(paragraph)
    root = html.fragment_fromstring(content or '<p></p>', create_parent='div')
    for img in root.xpath('.//img'):
        key = img.get('src')
        if key not in assets:
            raise ValueError('Unknown image')
        # Embedded image data only: no network or local file paths reach fpdf2.
        img.set('src', 'data:image/png;base64,' + b64encode(assets[key]).decode('ascii'))
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(assets[key])) as image:
            img.set('width', str(min(image.width * .75, (width - margins[0] - margins[2]) * pdf.k)))
    for node in root:
        text = normalize(node.text_content())
        if node.tag == 'p' and not text and not node.xpath('.//img'):
            # Empty paragraphs are real 12pt / 1.5 lines, including consecutive ones.
            height = 18 / pdf.k
            if pdf.will_page_break(height):
                pdf.add_page()
            pdf.ln(height)
            continue
        paragraph = paragraphs[text].popleft() if paragraphs[text] else None
        size, line_height, before, after, indent = 12, 1.5, 0, 0, 12.5
        if paragraph is not None:
            style = paragraph.style
            font_size = next((r.font.size for r in paragraph.runs if r.text.strip() and r.font.size is not None), None)
            while font_size is None and style is not None:
                font_size = style.font.size
                style = style.base_style
            if font_size is not None:
                size = max(6, min(font_size.pt, 72))
            spacing = inherited(paragraph, 'line_spacing')
            if spacing is not None:
                line_height = spacing.pt / size if hasattr(spacing, 'pt') else float(spacing)
                line_height = max(.8, min(line_height, 4))
            for attr in ('space_before', 'space_after'):
                value = inherited(paragraph, attr)
                if value is not None:
                    if attr == 'space_before': before = value.mm
                    else: after = value.mm
            value = inherited(paragraph, 'first_line_indent')
            if value is not None: indent = max(0, min(value.mm, 40))
            align = inherited(paragraph, 'alignment')
            if node.tag in ('p','h1','h2','h3','h4','h5','h6'):
                node.set('align', {0:'left',1:'center',2:'right',3:'justify'}.get(align, 'left'))
            if inherited(paragraph, 'page_break_before') and pdf.y > pdf.t_margin + 1:
                pdf.add_page()
        if node.tag in ('p','h1','h2','h3','h4','h5','h6'):
            node.set('line-height', str(line_height))
            node.set('data-indent', str(indent))
            node.set('data-before', str(before))
        pdf.set_font('Document', size=size)
        styles = {tag: TextStyle(font_family='Document', font_size_pt=size,
                                font_style='B' if tag.startswith('h') else '', color=0,
                                t_margin=before, b_margin=after,
                                l_margin=0)
                  for tag in ('p','h1','h2','h3','h4','h5','h6','pre','code')}
        pdf.write_html(html.tostring(node, encoding='unicode'), tag_styles=styles)
    pdf.output(target)
