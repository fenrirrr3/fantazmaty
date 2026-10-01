"""Recreate supported prose using new DOCX objects, never copying source parts."""
from io import BytesIO
from collections import Counter
from copy import deepcopy
from docx.oxml import OxmlElement
from docx.shared import RGBColor
from lxml import etree
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm
from docx.text.paragraph import Paragraph
from docx.text.run import Run

class RebuildUnsupported(ValueError):
    def __init__(self, omissions):
        self.omissions = [f"{label} (liczba: {count})" for label, count in sorted(omissions.items())]
        super().__init__("; ".join(self.omissions))


class Omissions(Counter):
    def add(self, label):
        self[label] += 1


def _format_resolver(original, omissions):
    # Resolve document defaults and theme fonts/colors without copying source parts.
    default_p = OxmlElement('w:p'); default_r = OxmlElement('w:r')
    defaults = original.styles.element.find(qn('w:docDefaults'))
    if defaults is not None:
        for path, target in (('w:rPrDefault/w:rPr',default_r), ('w:pPrDefault/w:pPr',default_p)):
            node = defaults
            for part in path.split('/'):
                node = node.find(qn(part)) if node is not None else None
            if node is not None: target.append(deepcopy(node))
    default_font = Run(default_r, Paragraph(default_p, original)).font
    default_format = Paragraph(default_p, original).paragraph_format
    theme = None
    for rel in original.part.rels.values():
        if rel.reltype.endswith('/theme') and not rel.is_external:
            theme = etree.fromstring(rel.target_part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
    ns = {'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
    def theme_font(key):
        if theme is None: return None
        family = 'majorFont' if key.startswith('major') else 'minorFont'
        nodes = theme.findall('.//a:fontScheme/a:' + family + '/a:latin', ns)
        return nodes[0].get('typeface') if nodes else None
    def font_name(fonts):
        for font in fonts:
            props = font._element.rPr
            node = props.find(qn('w:rFonts')) if props is not None else None
            if node is not None:
                key = node.get(qn('w:asciiTheme')) or node.get(qn('w:hAnsiTheme'))
                if key:
                    value = theme_font(key)
                    if value: return value
                    omissions.add('nierozpoznany krój czcionki z motywu (zastosujemy domyślny)')
                value = node.get(qn('w:ascii')) or node.get(qn('w:hAnsi'))
                if value: return value
        return None
    def font_color(fonts):
        for font in fonts:
            props=font._element.rPr
            node=props.find(qn('w:color')) if props is not None else None
            if node is None: continue
            value=node.get(qn('w:val')); key=node.get(qn('w:themeColor'))
            if key and theme is not None:
                key={'text1':'dk1','text2':'dk2','background1':'lt1','background2':'lt2'}.get(key,key)
                color=theme.find('.//a:clrScheme/a:'+key,ns)
                if color is not None and len(color): value=color[0].get('lastClr') or color[0].get('val')
            if value and len(value)==6 and all(c in '0123456789abcdefABCDEF' for c in value):
                rgb=[int(value[i:i+2],16) for i in (0,2,4)]
                shade=node.get(qn('w:themeShade')); tint=node.get(qn('w:themeTint'))
                if shade: rgb=[round(c*int(shade,16)/255) for c in rgb]
                if tint: rgb=[round(c+(255-c)*(1-int(tint,16)/255)) for c in rgb]
                return RGBColor(*rgb)
            if value == 'auto': return None
        return None
    return default_font, default_format, font_name, font_color


def _sanitize_body(body, omissions):
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
    for paragraph in body.iter(qn('w:p')):
        for path, label in (
            ('./w:pPr/w:pBdr', 'obramowania akapitów'),
            ('./w:pPr/w:shd', 'cieniowanie akapitów'),
            ('./w:pPr/w:framePr', 'ramki akapitów'),
            ('./w:pPr/w:textDirection', 'niestandardowy kierunek tekstu'),
            ('.//w:rPr/w:spacing', 'rozstrzelenie znaków'),
            ('.//w:rPr/w:shd', 'cieniowanie znaków'),
            ('.//w:rPr/w:bdr', 'obramowania znaków'),
        ):
            for _ in paragraph.xpath(path): omissions.add(label)


def styles(style):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id); yield style; style = style.base_style
def effective(objects, name):
    for obj in objects:
        value = getattr(obj, name)
        if value is not None: return value
    return None


def _paragraph_specs(original, omissions):
    body = original.element.body
    default_font, default_format, font_name, font_color = _format_resolver(original, omissions)
    paragraphs = []
    for element in body:
        if element.tag == qn('w:sectPr'): continue
        if element.tag != qn('w:p'):
            omissions.add('blok ' + etree.QName(element).localname + ' wraz z treścią')
            continue
        old = Paragraph(element, original)
        paragraph_styles = list(styles(old.style))
        if element.xpath('./w:pPr/w:numPr') or any(s.element.xpath('./w:pPr/w:numPr') for s in paragraph_styles):
            omissions.add('automatyczne numerowanie i punktory list (tekst pozycji pozostanie)')
        heading = next((st.name for st in paragraph_styles if st.name.startswith('Heading ') and st.name[8:].isdigit()), None)
        paragraph = {'heading': heading, 'format': {}, 'tabs': [], 'runs': []}
        paragraphs.append(paragraph)
        formats = [old.paragraph_format] + [s.paragraph_format for s in paragraph_styles] + [default_format]
        for name in ('alignment','first_line_indent','left_indent','right_indent','space_before','space_after',
                     'line_spacing','line_spacing_rule','keep_together','keep_with_next','page_break_before','widow_control'):
            value = effective(formats, name)
            if value is not None:
                paragraph['format'][name] = value
        for formatting in formats:
            if len(formatting.tab_stops):
                for stop in formatting.tab_stops:
                    paragraph['tabs'].append((stop.position, stop.alignment, stop.leader))
                break
        for element_run in element.iter(qn('w:r')):
            old_run = Run(element_run, old)
            fonts = [old_run.font] + [s.font for s in styles(old_run.style)] + [s.font for s in paragraph_styles] + [default_font]
            if effective(fonts, 'hidden'): continue
            new_run = {'content': [], 'font': {}}
            paragraph['runs'].append(new_run)
            for child in element_run:
                tag = child.tag
                if tag == qn('w:rPr'): continue
                if tag == qn('w:t'): new_run['content'].append(('text', child.text or ''))
                elif tag == qn('w:tab'): new_run['content'].append(('tab', None))
                elif tag in (qn('w:br'),qn('w:cr')):
                    from docx.enum.text import WD_BREAK
                    kind = child.get(qn('w:type'),'textWrapping')
                    new_run['content'].append(('break', {'page':WD_BREAK.PAGE,'column':WD_BREAK.COLUMN}.get(kind,WD_BREAK.LINE)))
                elif tag == qn('w:noBreakHyphen'): new_run['content'].append(('text', '‑'))
                elif tag == qn('w:softHyphen'): new_run['content'].append(('text', '\u00ad'))
                elif tag in (qn('w:commentReference'),qn('w:lastRenderedPageBreak')): continue
                else: omissions.add('element ' + etree.QName(child).localname + ' wewnątrz tekstu')
            for name in ('size','bold','italic','underline','strike','double_strike',
                         'superscript','subscript','small_caps','all_caps','highlight_color'):
                value = effective(fonts,name)
                if value is not None:
                    new_run['font'][name] = value
            name = font_name(fonts)
            if name: new_run['font']['name'] = name
            color = font_color(fonts)
            if color is not None: new_run['color'] = color
    return paragraphs


def _analyze_docx(source):
    source.seek(0)
    original = Document(source)
    body = original.element.body
    omissions = Omissions()
    for rel in original.part.rels.values():
        if not rel.is_external and rel.reltype.endswith(('/header', '/footer')):
            root = etree.fromstring(rel.target_part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
            if any((node.text or '').strip() for node in root.iter(qn('w:t'))) or root.find('.//' + qn('w:drawing')) is not None:
                omissions.add('niepuste nagłówki lub stopki')
    if len(original.sections) > 1:
        omissions['podziały na sekcje i ustawienia ich układu (pozostanie jedna sekcja A4)'] = len(original.sections)-1
    for link in body.iter(qn('w:hyperlink')):
        omissions.add('odnośniki hiperłączy (widoczny tekst pozostanie)')
    _sanitize_body(body, omissions)
    return _paragraph_specs(original, omissions), omissions


def inspect_docx(source):
    """Return exactly the rebuild warnings without creating or saving an output DOCX."""
    _, omissions = _analyze_docx(source)
    if omissions:
        raise RebuildUnsupported(omissions)


def _new_document(paragraphs):
    result = Document()
    for section in result.sections:
        section.page_width = Cm(21)
        section.page_height = Cm(29.7)
        section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Cm(2.5)
    for item in paragraphs:
        heading = item['heading']
        paragraph = result.add_paragraph(style=heading if heading in result.styles else None)
        for name, value in item['format'].items():
            setattr(paragraph.paragraph_format, name, value)
        for stop in item['tabs']:
            paragraph.paragraph_format.tab_stops.add_tab_stop(*stop)
        for specification in item['runs']:
            run = paragraph.add_run()
            for kind, value in specification['content']:
                if kind == 'text': run.add_text(value)
                elif kind == 'tab': run.add_tab()
                else: run.add_break(value)
            for name, value in specification['font'].items():
                setattr(run.font, name, value)
            if 'color' in specification:
                run.font.color.rgb = specification['color']
    props = result.core_properties
    for name in ('author','last_modified_by','title','subject','comments','keywords','category','content_status','identifier','language','version'):
        setattr(props,name,'')
    for tag in ('dcterms:created', 'dcterms:modified', 'cp:lastPrinted'):
        for node in list(props._element.findall(qn(tag))):
            props._element.remove(node)
    props.revision = 1
    return result


def rebuild_docx(source, *, allow_omissions=False):
    paragraphs, omissions = _analyze_docx(source)
    if omissions and not allow_omissions:
        raise RebuildUnsupported(omissions)
    result = _new_document(paragraphs)
    output = BytesIO()
    result.save(output)
    output.seek(0)
    return output
