"""Normalize conversion typography without replacing text or inline markup.

Śledzone zmiany i komentarze zostają nietknięte. Poprzednie formatowanie zapisane w zmianach formatowania
(w:rPrChange, w:pPrChange) jest ujednolicane tak samo jak obecne: zmiana pokazuje wtedy tylko to, co zmienił
redaktor, a jej odrzucenie nie cofa ujednolicenia."""
from copy import deepcopy
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
        try:
            styles = [style.element for style in style_chain(paragraph_style(paragraph))]
        except ValueError:
            styles = []  # styl o błędnym typie (uszkodzony DOCX) – liczy się tylko sam akapit
        owners = [paragraph._p, *styles]
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


def _schema_order(properties):
    """Elementy właściwości w kolejności schematu, gdy wszystkie są znane – np. LibreOffice zapisuje w:pPrChange
    przed w:rPr, a Word odrzuca taki dokument jako uszkodzony."""
    sequence = _ORDER.get(properties.tag, ())
    if any(child.tag not in sequence for child in properties):
        return
    ordered = sorted(properties, key=lambda child: sequence.index(child.tag))
    if ordered != list(properties):
        for child in ordered:
            properties.append(child)


def _normalize_paragraph(props, alignment, *, justify, margin_story, in_table, listed):
    """Wyrównanie (justowanie), odstępy i wcięcie pierwszego wiersza we właściwościach akapitu."""
    def child(tag):
        return _child(props, "w:" + tag)

    # Justujemy tylko tekst wyrównany do lewej lub bez wyrównania; środek, prawa
    # strona i nagłówki/stopki (numer strony, tytuł) zostają jak w źródle.
    if justify and not margin_story and not in_table and alignment in (None, 0):
        child("jc").set(qn("w:val"), "both")
    if not in_table:
        spacing = child("spacing")
        spacing.attrib.clear()
        for key, value in dict(before="0", after="0", line="360", lineRule="auto").items():
            spacing.set(qn("w:" + key), value)
        child("contextualSpacing").set(qn("w:val"), "0")
    if not in_table and not listed:
        indent = child("ind")
        for key in ("firstLineChars", "hanging", "hangingChars"):
            indent.attrib.pop(qn("w:" + key), None)
        indent.set(qn("w:firstLine"), "0" if alignment == 1 else "709")
    _schema_order(props)


def _normalize_run(run, rpr):
    """Times New Roman 12 pt we właściwościach przebiegu (rpr – obecne albo poprzednie z w:rPrChange)."""
    # _child zachowuje kolejność schematu (w:rPrChange na końcu), python-docx dopisałby w:rFonts za nią.
    fonts = _child(rpr, "w:rFonts")
    for key in list(fonts.attrib):
        if key.endswith("Theme"):
            del fonts.attrib[key]
    if not _symbol_run(run, fonts):
        for key in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn("w:" + key), "Times New Roman")
    for tag in ("sz", "szCs"):
        size = _child(rpr, "w:" + tag)
        size.set(qn("w:val"), "24")
    _schema_order(rpr)


def _previous_properties(owner, change, tag):
    """Poprzednie właściwości ze zmiany formatowania (tworzone, gdy edytor ich nie zapisał – np. LibreOffice
    zapisuje pusty w:pPrChange)."""
    node = owner.find(qn(change))
    if node is None:
        return None
    properties = node.find(qn(tag))
    if properties is None:
        properties = OxmlElement(tag)
        node.append(properties)
    return properties


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
                    props = element.get_or_add_pPr()
                    # Komórki tabel zachowują swój układ; listy i nagłówki zachowują wcięcia
                    # (wcięcie listy pochodzi z numeracji, a akapit by je nadpisał).
                    options = dict(justify=justify, margin_story=margin_story,
                                   in_table=any(parent.tag == qn("w:tc") for parent in element.iterancestors()))
                    previous = _previous_properties(props, "w:pPrChange", "w:pPr")
                    if previous is not None:
                        # Poprzednie formatowanie akapitu: wyrównanie i lista według niego samego.
                        before = OxmlElement("w:p")
                        before.append(deepcopy(previous))
                        before = Paragraph(before, document)
                        _normalize_paragraph(previous, _alignment(before), listed=heading(before), **options)
                    _normalize_paragraph(props, _alignment(paragraph), listed=heading(paragraph), **options)
                    # Includes hyperlinks and runs inside other inline containers.
                    for run in element.iter(qn("w:r")):
                        rpr = run.get_or_add_rPr()
                        _normalize_run(run, rpr)
                        previous = _previous_properties(rpr, "w:rPrChange", "w:rPr")
                        if previous is not None:
                            _normalize_run(run, previous)
                data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            outgoing.writestr(entry, data)
    source.seek(0)
    output.seek(0)
    return output
