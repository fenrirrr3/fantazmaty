"""Normalize conversion typography without replacing text or inline markup."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree
if __package__:
    from .document_styles import paragraph_property
else:
    from document_styles import paragraph_property


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
                    size = section.find(qn('w:pgSz'))
                    if size is None:
                        size = etree.SubElement(section, qn("w:pgSz"))
                    size.set(qn("w:w"), "11906")
                    size.set(qn("w:h"), "16838")
                    size.set(qn("w:orient"), "portrait")
                    margins = section.find(qn("w:pgMar"))
                    if margins is None:
                        margins = etree.SubElement(section, qn("w:pgMar"))
                    for side in ("top", "bottom", "left", "right"):
                        margins.set(qn("w:" + side), "1417")
                    margins.set(qn("w:gutter"), "0")
                for element in tree.iter(qn("w:p")):
                    paragraph = Paragraph(element, document)
                    alignment = paragraph_property(paragraph, "alignment")
                    props = element.get_or_add_pPr()

                    def child(tag):
                        item = props.find(qn("w:" + tag))
                        if item is None:
                            item = etree.SubElement(props, qn("w:" + tag))
                        return item

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
                            size = rpr.find(qn("w:" + tag))
                            if size is None:
                                size = etree.SubElement(rpr, qn("w:" + tag))
                            size.set(qn("w:val"), "24")
                data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            outgoing.writestr(entry, data)
    source.seek(0)
    output.seek(0)
    return output
