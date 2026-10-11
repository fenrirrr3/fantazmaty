"""Normalize conversion typography without replacing text or inline markup."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree
if __package__:
    from .document_styles import paragraph_property, paragraph_style, style_chain
else:
    from document_styles import paragraph_property, paragraph_style, style_chain

# Word odrzuca dokument, w którym elementy właściwości stoją poza kolejnością
# schematu (np. w:spacing po w:rPr akapitu albo w:sz po w:lang).
_SEQUENCES = {
    'w:pPr': 'pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd '
             'tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi '
             'adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc '
             'textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange',
    'w:rPr': 'rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof '
             'snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd '
             'fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath rPrChange',
    'w:sectPr': 'headerReference footerReference footnotePr endnotePr type pgSz pgMar paperSrc pgBorders '
                'lnNumType pgNumType cols formProt vAlign noEndnote titlePg textDirection bidi rtlGutter '
                'docGrid printerSettings sectPrChange',
}
_ORDER = {qn(tag): [qn('w:' + name) for name in names.split()] for tag, names in _SEQUENCES.items()}


def _child(parent, tag):
    """Istniejący albo nowy element tag w miejscu wymaganym przez schemat WordprocessingML."""
    name = qn(tag)
    found = parent.find(name)
    if found is not None:
        return found
    element = OxmlElement(tag)
    sequence = _ORDER.get(parent.tag, ())
    later = set(sequence[sequence.index(name) + 1:]) if name in sequence else set()
    for sibling in parent:
        if sibling.tag in later:
            sibling.addprevious(element)
            return element
    parent.append(element)
    return element


# Wartości w:jc spoza słownika python-docx (np. „start”/„end” z nowszych edytorów).
_RAW_ALIGNMENT = {'start': 0, 'left': 0, 'center': 1, 'end': 2, 'right': 2, 'both': 3}


def _alignment(paragraph):
    """Wyrównanie akapitu z dziedziczeniem; None = brak (do lewej). Liczby jak WD_ALIGN_PARAGRAPH."""
    try:
        return paragraph_property(paragraph, "alignment")
    except ValueError:
        owners = [paragraph._p, *(style.element for style in style_chain(paragraph_style(paragraph)))]
        for owner in owners:
            jc = owner.find(f'{qn("w:pPr")}/{qn("w:jc")}')
            if jc is not None:
                return _RAW_ALIGNMENT.get(jc.get(qn("w:val")), 3)
        return None


SYMBOL_FONTS = ('symbol', 'wingdings', 'webdings', 'marlett', 'mt extra')


def _symbol_run(run, fonts):
    """Przebieg w foncie symbolicznym (albo ze znakami z obszaru prywatnego) zachowuje swój font."""
    names = (fonts.get(qn("w:" + key), "") for key in ("ascii", "hAnsi", "cs", "eastAsia"))
    if any(name.lower() in SYMBOL_FONTS for name in names):
        return True
    return any(0xF000 <= ord(char) <= 0xF0FF for node in run.iter(qn("w:t")) for char in (node.text or ""))


def _heading_lookup(document):
    """Funkcja: akapit → czy to nagłówek albo punkt listy (bezpośrednio lub ze stylu, z dziedziczeniem)."""
    def owned(element):
        props = element.find(qn("w:pPr"))
        if props is None:
            return None
        numbering = props.find(f'{qn("w:numPr")}/{qn("w:numId")}')
        if numbering is not None:
            return numbering.get(qn("w:val")) != "0"
        level = props.find(qn("w:outlineLvl"))
        if level is not None:
            return level.get(qn("w:val")) != "9"
        return None

    def is_heading_or_list(paragraph):
        own = owned(paragraph._p)
        if own is not None:
            return own
        try:
            chain = list(style_chain(paragraph_style(paragraph)))
        except (KeyError, ValueError, AttributeError):
            return False
        for style in chain:
            name = (style.name or "").lower()
            if name.startswith(("heading", "nagłówek", "naglowek")) or (style.style_id or "").lower().startswith("heading"):
                return True
            inherited = owned(style.element)
            if inherited is not None:
                return inherited
        return False
    return is_heading_or_list


def normalize_docx(source, *, justify=False):
    source.seek(0)
    document = Document(source)
    source.seek(0)
    output = BytesIO()
    with ZipFile(source) as incoming, ZipFile(output, 'w', ZIP_DEFLATED) as outgoing:
        for entry in incoming.infolist():
            data = incoming.read(entry.filename)
            name = entry.filename
            story = name in ('word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml') or (
                name.startswith(('word/header', 'word/footer')) and name.endswith('.xml'))
            if story:
                margin_story = name.startswith(('word/header', 'word/footer'))
                # python-docx's parser supplies typed elements for Paragraph/style access.
                from docx.oxml import parse_xml
                tree = parse_xml(data)
                for section in tree.iter(qn('w:sectPr')):
                    size = _child(section, "w:pgSz")
                    size.set(qn("w:w"), "11906")
                    size.set(qn("w:h"), "16838")
                    size.set(qn("w:orient"), "portrait")
                    margins = _child(section, "w:pgMar")
                    for side in ("top", "bottom", "left", "right"):
                        margins.set(qn("w:" + side), "1417")
                    margins.set(qn("w:gutter"), "0")
                    # Schemat wymaga także odległości nagłówka i stopki (domyślne Worda: 1,25 cm).
                    for side in ("header", "footer"):
                        if margins.get(qn("w:" + side)) is None:
                            margins.set(qn("w:" + side), "708")
                heading = _heading_lookup(document)
                for element in tree.iter(qn("w:p")):
                    paragraph = Paragraph(element, document)
                    alignment = _alignment(paragraph)
                    props = element.get_or_add_pPr()
                    # Komórki tabel zachowują swój układ; listy i nagłówki zachowują wcięcia
                    # (wcięcie listy pochodzi z numeracji, a akapit by je nadpisał).
                    in_table = any(parent.tag == qn("w:tc") for parent in element.iterancestors())
                    listed = heading(paragraph)

                    def child(tag):
                        return _child(props, "w:" + tag)

                    # Justujemy tylko tekst wyrównany do lewej lub bez wyrównania; środek, prawa
                    # strona i nagłówki/stopki (numer strony, tytuł) zostają jak w źródle.
                    if justify and not margin_story and not in_table and alignment in (None, 0):
                        child("jc").set(qn("w:val"), "both")
                    if not in_table:
                        spacing = child("spacing")
                        spacing.attrib.clear()
                        for key, value in dict(
                            before="0", after="0", line="360", lineRule="auto"
                        ).items():
                            spacing.set(qn("w:" + key), value)
                        child("contextualSpacing").set(qn("w:val"), "0")
                    if not in_table and not listed:
                        indent = child("ind")
                        for key in ("firstLineChars", "hanging", "hangingChars"):
                            indent.attrib.pop(qn("w:" + key), None)
                        indent.set(qn("w:firstLine"), "0" if alignment == 1 else "709")
                    # Includes hyperlinks and runs inside other inline containers.
                    for run in element.iter(qn("w:r")):
                        rpr = run.get_or_add_rPr()
                        fonts = rpr.get_or_add_rFonts()
                        for key in list(fonts.attrib):
                            if key.endswith("Theme"):
                                del fonts.attrib[key]
                        if not _symbol_run(run, fonts):
                            for key in ("ascii", "hAnsi", "eastAsia", "cs"):
                                fonts.set(qn("w:" + key), "Times New Roman")
                        for tag in ("sz", "szCs"):
                            size = _child(rpr, "w:" + tag)
                            size.set(qn("w:val"), "24")
                data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            outgoing.writestr(entry, data)
    source.seek(0)
    output.seek(0)
    return output
