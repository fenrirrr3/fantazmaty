"""Recreate supported prose using new DOCX objects, never copying source parts."""
from io import BytesIO
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm
from docx.text.paragraph import Paragraph
from docx.text.run import Run

class RebuildUnsupported(ValueError):
    def __init__(self, omissions):
        self.omissions = sorted(set(omissions))
        super().__init__("; ".join(self.omissions))


def rebuild_docx(source, *, allow_omissions=False):
    source.seek(0)
    original = Document(source)
    body = original.element.body
    omissions = set()
    # Accept inline revisions: retained runs lose reviewer IDs and timestamps.
    for tag in ('del', 'moveFrom'):
        for node in list(body.iter(qn('w:' + tag))):
            if node.getparent() is not None:
                if node.getparent().tag == qn('w:rPr'):
                    omissions.add('oznaczenia usunięcia akapitu (akapit pozostanie)')
                node.getparent().remove(node)
    for tag in ('ins', 'moveTo'):
        for node in list(body.iter(qn('w:' + tag))):
            parent = node.getparent()
            if parent is not None:
                if parent.tag in (qn('w:rPr'), qn('w:pPr')):
                    omissions.add('oznaczenia zmian akapitów (podział na akapity pozostanie)')
                index = parent.index(node)
                for child in list(node):
                    parent.insert(index, child); index += 1
                parent.remove(node)
    unsupported = {
        'tbl': 'tabele wraz z ich treścią', 'drawing': 'ilustracje i pola tekstowe wraz z ich treścią',
        'pict': 'ilustracje i pola tekstowe wraz z ich treścią', 'object': 'obiekty osadzone',
        'footnoteReference': 'przypisy dolne i ich odsyłacze', 'endnoteReference': 'przypisy końcowe i ich odsyłacze',
        'fldChar': 'mechanizm pól automatycznych (zwykły tekst wyniku może pozostać)',
        'instrText': 'instrukcje pól automatycznych', 'fldSimple': 'pola automatyczne wraz z ich treścią',
        'altChunk': 'dołączona treść zewnętrzna', 'sdt': 'kontrolki zawartości wraz z ich treścią',
        'sym': 'symbole specjalne zapisane jako obiekty',
    }
    for tag, label in unsupported.items():
        for node in list(body.iter(qn('w:' + tag))):
            omissions.add(label)
            if node.getparent() is not None: node.getparent().remove(node)
    for node in list(body.iter()):
        if node.tag.startswith('{http://schemas.openxmlformats.org/officeDocument/2006/math}'):
            omissions.add('równania matematyczne')
            if node.getparent() is not None: node.getparent().remove(node)
    result = Document()
    for section in result.sections:
        section.page_width = Cm(21); section.page_height = Cm(29.7)
        section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Cm(2.5)
    def styles(style):
        seen = set()
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id); yield style; style = style.base_style
    def effective(objects, name):
        for obj in objects:
            value = getattr(obj, name)
            if value is not None: return value
        return None
    for element in body:
        if element.tag == qn('w:sectPr'): continue
        if element.tag != qn('w:p'):
            omissions.add('nieobsługiwane bloki dokumentu wraz z ich treścią')
            continue
        old = Paragraph(element, original)
        paragraph_styles = list(styles(old.style))
        if element.xpath('./w:pPr/w:numPr') or any(s.element.xpath('./w:pPr/w:numPr') for s in paragraph_styles):
            omissions.add('automatyczne numerowanie i punktory list (tekst pozycji pozostanie)')
        paragraph = result.add_paragraph()
        formats = [old.paragraph_format] + [s.paragraph_format for s in paragraph_styles]
        for name in ('alignment','first_line_indent','left_indent','right_indent','space_before','space_after',
                     'line_spacing','line_spacing_rule','keep_together','keep_with_next','page_break_before','widow_control'):
            value = effective(formats, name)
            if value is not None: setattr(paragraph.paragraph_format, name, value)
        for formatting in formats:
            if len(formatting.tab_stops):
                for stop in formatting.tab_stops:
                    paragraph.paragraph_format.tab_stops.add_tab_stop(stop.position,stop.alignment,stop.leader)
                break
        for element_run in element.iter(qn('w:r')):
            old_run = Run(element_run, old)
            fonts = [old_run.font] + [s.font for s in styles(old_run.style)] + [s.font for s in paragraph_styles]
            if effective(fonts, 'hidden'): continue
            new_run = paragraph.add_run()
            for child in element_run:
                tag = child.tag
                if tag == qn('w:rPr'): continue
                if tag == qn('w:t'): new_run.add_text(child.text or '')
                elif tag == qn('w:tab'): new_run.add_tab()
                elif tag in (qn('w:br'),qn('w:cr')):
                    from docx.enum.text import WD_BREAK
                    kind = child.get(qn('w:type'),'textWrapping')
                    new_run.add_break({'page':WD_BREAK.PAGE,'column':WD_BREAK.COLUMN}.get(kind,WD_BREAK.LINE))
                elif tag == qn('w:noBreakHyphen'): new_run.add_text('‑')
                elif tag == qn('w:softHyphen'): new_run.add_text('\u00ad')
                elif tag in (qn('w:commentReference'),qn('w:lastRenderedPageBreak')): continue
                else: omissions.add('nieobsługiwane elementy wewnątrz tekstu')
            for name in ('name','size','bold','italic','underline','strike','double_strike',
                         'superscript','subscript','small_caps','all_caps','highlight_color'):
                value = effective(fonts,name)
                if value is not None: setattr(new_run.font,name,value)
            for font in fonts:
                if font.color.rgb is not None:
                    new_run.font.color.rgb = font.color.rgb; break
    props = result.core_properties
    for name in ('author','last_modified_by','title','subject','comments','keywords','category','content_status','identifier','language','version'):
        setattr(props,name,'')
    for tag in ('dcterms:created', 'dcterms:modified', 'cp:lastPrinted'):
        for node in list(props._element.findall(qn(tag))):
            props._element.remove(node)
    props.revision = 1
    if omissions and not allow_omissions:
        raise RebuildUnsupported(omissions)
    output = BytesIO(); result.save(output); output.seek(0)
    return output
