"""Normalize conversion typography without replacing text or inline markup."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree
if __package__:
    from .document_styles import paragraph_property
else:
    from document_styles import paragraph_property

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
                for element in tree.iter(qn("w:p")):
                    paragraph = Paragraph(element, document)
                    alignment = paragraph_property(paragraph, "alignment")
                    props = element.get_or_add_pPr()

                    def child(tag):
                        return _child(props, "w:" + tag)

                    if justify and alignment != 1:
                        child("jc").set(qn("w:val"), "both")
                    spacing = child("spacing")
                    spacing.attrib.clear()
                    for key, value in dict(
                        before="0", after="0", line="360", lineRule="auto"
                    ).items():
                        spacing.set(qn("w:" + key), value)
                    indent = child("ind")
                    for key in ("firstLineChars", "hanging", "hangingChars"):
                        indent.attrib.pop(qn("w:" + key), None)
                    indent.set(qn("w:firstLine"), "0" if alignment == 1 else "709")
                    child("contextualSpacing").set(qn("w:val"), "0")
                    # Includes hyperlinks and runs inside other inline containers.
                    for run in element.iter(qn("w:r")):
                        rpr = run.get_or_add_rPr()
                        fonts = rpr.get_or_add_rFonts()
                        for key in list(fonts.attrib):
                            if key.endswith("Theme"):
                                del fonts.attrib[key]
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
