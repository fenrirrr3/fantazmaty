"""Syntetyczne DOCX z rzadszych edytorów i z problematycznymi elementami OOXML (scenariusze skrajne).

Uzupełnia editor_fixtures (LibreOffice 24.2 i Google Docs). Pliki są budowane od zera (ZipFile + ręczny XML),
żeby odtworzyć zapis, którego python-docx sam nie wytworzy. Źródło cech:

* próbki prawdziwe (wygenerowane przy przeglądzie 2026-10, niedołączone do repo): LibreOffice 24.2.7 (bogaty
  dokument wyeksportowany filtrami „MS Word 2007 XML”, „Office Open XML Text”, „MS Word 97”, „Rich Text
  Format”, „MS Word 2003 XML”, „MS Word 2007 XML Template”, z hasłem i bez) i pandoc 3.1.3 (Markdown z
  przypisami, tabelą, wzorami i spisem treści). Z próbek pochodzą: pole DATE z ``w:fldLock``, spis treści
  w ``w:sdt`` z galerii „Table of Contents”, lista rozwijana jako ``w:sdt`` z ``w:dropDownList``, ``w:sym``
  z Wingdings, obraz SVG jako ``asvg:svgBlip`` z zapasowym PNG, wykres ``c:chart``, przypisy z ``w:id``
  20 i 21 (pandoc), ``m:oMathPara`` (pandoc), szablon z typem ``…template.main+xml``, sygnatury plików
  ``.doc``/RTF/ODT/XML Worda 2003 i zaszyfrowanego DOCX (kontener OLE ze strumieniem ``EncryptedPackage``);
* znajomość formatu (ECMA-376, [MS-DOCX], znane eksporty) – reszta, opisana przy funkcji. Cechy WPS,
  OnlyOffice, Apple Pages i minimalnych eksporterów (AbiWord, Calligra) są przybliżone: odtwarzają to, czym
  ich zapis różni się od Worda i co ma znaczenie dla programów CMS.

Apache OpenOffice 4.x nie zapisuje DOCX (tylko go czyta): jego pliki przychodzą jako ``.doc``, ``.odt``,
``.rtf`` albo XML Worda 2003 – czasem z nazwą zmienioną na ``.docx`` (``foreign_file``). „Starszy OOXML”
(Word 2007, OpenOffice.org 3) odtwarza ``word2007_docx``.

Słowniki na końcu modułu: ``EDITOR_SCENARIOS`` i ``FEATURE_SCENARIOS`` – pliki, które programy mają przetworzyć
bez cichej utraty tekstu; ``REFUSED_SCENARIOS`` – pliki odrzucane przed pracą programów z komunikatem.
"""
import warnings
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .editor_fixtures import R_NS, WORD_PART, W_NS, XML_HEADER, _content_types, _rels

OFFICE = 'http://schemas.microsoft.com/office'
# Przestrzenie nazw Worda 365 (Windows, Mac, Online). mc:Ignorable wymienia też rozszerzenia nowsze od
# bibliotek (w16du z 2023 r., w16sdtfl) i jedno nieznane nikomu (w16xyz) – wolno je pominąć.
WORD365_NAMESPACES = (
    'xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" '
    f'xmlns:r="{R_NS}" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:w10="urn:schemas-microsoft-com:office:word" '
    f'xmlns:w="{W_NS}" '
    f'xmlns:w14="{OFFICE}/word/2010/wordml" '
    f'xmlns:w15="{OFFICE}/word/2012/wordml" '
    f'xmlns:w16cex="{OFFICE}/word/2018/wordml/cex" '
    f'xmlns:w16cid="{OFFICE}/word/2016/wordml/cid" '
    f'xmlns:w16="{OFFICE}/word/2018/wordml" '
    f'xmlns:w16du="{OFFICE}/word/2023/wordml/word16du" '
    f'xmlns:w16sdtdh="{OFFICE}/word/2020/wordml/sdtdatahash" '
    f'xmlns:w16sdtfl="{OFFICE}/word/2024/wordml/sdtformatlock" '
    f'xmlns:w16se="{OFFICE}/word/2015/wordml/symex" '
    'xmlns:w16xyz="http://example.test/word/2030/nieznane" '
    f'xmlns:wpg="{OFFICE}/word/2010/wordprocessingGroup" '
    f'xmlns:wne="{OFFICE}/word/2006/wordml" '
    f'xmlns:wps="{OFFICE}/word/2010/wordprocessingShape" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
    f'xmlns:asvg="{OFFICE}/drawing/2016/SVG/main" '
    'mc:Ignorable="w14 w15 w16se w16cid w16 w16cex w16sdtdh w16sdtfl w16du w16xyz wp14"'
)
SECTION = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1417" w:right="1417" w:bottom="1417" '
           'w:left="1417" w:header="708" w:footer="708" w:gutter="0"/><w:cols w:space="708"/></w:sectPr>')
# Style polskiego Worda: identyfikatory zlokalizowane (Normalny, Nagwek1), nazwy wbudowane po angielsku.
STYLES = (
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi"/>'
    '<w:sz w:val="24"/><w:lang w:val="pl-PL"/></w:rPr></w:rPrDefault><w:pPrDefault/></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normalny"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Nagwek1"><w:name w:val="heading 1"/><w:basedOn w:val="Normalny"/>'
    '<w:next w:val="Normalny"/><w:qFormat/><w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
    '<w:style w:type="character" w:default="1" w:styleId="Domylnaczcionkaakapitu"><w:name w:val="Default '
    'Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
    '<w:style w:type="table" w:default="1" w:styleId="Standardowy"><w:name w:val="Normal Table"/>'
    '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/></w:tblPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Legenda"><w:name w:val="caption"/><w:basedOn w:val="Normalny"/>'
    '<w:rPr><w:i/></w:rPr></w:style>'
    '<w:style w:type="character" w:styleId="Tekstzastpczy"><w:name w:val="Placeholder Text"/>'
    '<w:rPr><w:color w:val="666666"/></w:rPr></w:style>'
)
THEME = (
    f'{XML_HEADER}<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Pakiet Office">'
    '<a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="0E2841"/></a:dk2>'
    '<a:lt2><a:srgbClr val="E8E8E8"/></a:lt2><a:accent1><a:srgbClr val="156082"/></a:accent1>'
    '<a:accent2><a:srgbClr val="E97132"/></a:accent2><a:accent3><a:srgbClr val="196B24"/></a:accent3>'
    '<a:accent4><a:srgbClr val="0F9ED5"/></a:accent4><a:accent5><a:srgbClr val="A02B93"/></a:accent5>'
    '<a:accent6><a:srgbClr val="4EA72E"/></a:accent6><a:hlink><a:srgbClr val="467886"/></a:hlink>'
    '<a:folHlink><a:srgbClr val="96607D"/></a:folHlink></a:clrScheme><a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Aptos"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
    '</a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/>'
    '</a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/>'
    '</a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>'
    '</a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>')
COMPAT_15 = ('<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" '
             'w:val="15"/></w:compat>')
# Typ zawartości części głównej: dokument, dokument z makrami, szablon i szablon z makrami.
DOCUMENT_TYPES = {
    'docx': WORD_PART.format('document.main'),
    'docm': 'application/vnd.ms-word.document.macroEnabled.main+xml',
    'dotx': WORD_PART.format('template.main'),
    'dotm': 'application/vnd.ms-word.template.macroEnabledTemplate.main+xml',
}
# Najmniejszy poprawny PNG (1×1 px).
PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
       b'\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82')
# Nagłówki formatów, których Pillow nie rasteryzuje (EMF/WMF z Worda, HEIC z iPhone'a) – reszta pliku zbędna.
EMF = b'\x01\x00\x00\x00' + b'\x00' * 36 + b' EMF' + b'\x00' * 48
WMF = b'\xd7\xcd\xc6\x9a' + b'\x00' * 40
HEIC = b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic' + b'\x00' * 16


def p(*runs, properties=''):
    """Akapit z podanych fragmentów XML (przebiegi, pola, kontrolki…)."""
    return f'<w:p>{f"<w:pPr>{properties}</w:pPr>" if properties else ""}' + ''.join(runs) + '</w:p>'


def r(text, properties=''):
    """Przebieg ze zwykłym tekstem (znaki specjalne XML zamienione na encje)."""
    space = ' xml:space="preserve"' if text != text.strip() else ''
    escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return f'<w:r>{f"<w:rPr>{properties}</w:rPr>" if properties else ""}<w:t{space}>{escaped}</w:t></w:r>'


def field(instruction, result, *, lock=False):
    """Pole złożone (w:fldChar) w jednym akapicie: kod pola i jego ostatni wynik."""
    locked = ' w:fldLock="1"' if lock else ''
    instruction = instruction.replace('&', '&amp;').replace('"', '&quot;')
    return (f'<w:r><w:fldChar w:fldCharType="begin"{locked}/></w:r><w:r><w:instrText xml:space="preserve"> '
            f'{instruction} </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>{r(result)}'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def drawing(graphic, *, anchor=False, name='Obraz 1'):
    """Rysunek DrawingML (wp:inline albo wp:anchor) z podanym a:graphicData."""
    if anchor:
        frame = ('<wp:anchor distT="0" distB="0" distL="114300" distR="114300" simplePos="0" '
                 'relativeHeight="251659264" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
                 '<wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset>'
                 '</wp:positionH><wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>')
        wrap, end = '<wp:wrapSquare wrapText="bothSides"/>', '</wp:anchor>'
    else:
        frame, wrap, end = '<wp:inline distT="0" distB="0" distL="0" distR="0">', '', '</wp:inline>'
    return (f'<w:r><w:drawing>{frame}<wp:extent cx="952500" cy="476250"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
            f'{wrap}<wp:docPr id="1" name="{name}"/><wp:cNvGraphicFramePr/><a:graphic>{graphic}</a:graphic>{end}'
            '</w:drawing></w:r>')


def picture(rid, *, link=False, extension=''):
    """a:graphicData obrazu (pic:pic); link – obraz połączony (r:link), extension – a:ext w a:blip (np. SVG)."""
    attribute = 'r:link' if link else 'r:embed'
    ext = f'<a:extLst>{extension}</a:extLst>' if extension else ''
    return ('<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
            '<pic:nvPicPr><pic:cNvPr id="0" name="obraz"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip {attribute}="{rid}">{ext}</a:blip><a:stretch><a:fillRect/></a:stretch>'
            '</pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="476250"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData>')


def text_box(*paragraphs):
    """Pole tekstowe Worda 2010+: kształt wps w mc:Choice i ta sama treść w zapasowym VML."""
    content = '<w:txbxContent>' + ''.join(paragraphs) + '</w:txbxContent>'
    shape = ('<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:wsp>'
             f'<wps:cNvSpPr txBox="1"/><wps:spPr/><wps:txbx>{content}</wps:txbx><wps:bodyPr/></wps:wsp>'
             '</a:graphicData>')
    return ('<w:r><mc:AlternateContent><mc:Choice Requires="wps">' + drawing(shape, anchor=True)[5:-6]
            + '</mc:Choice><mc:Fallback><w:pict><v:shape id="Pole1" style="width:120pt;height:30pt">'
            f'<v:textbox>{content}</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>')


def notes_part(kind, notes, *, separators=(-1, 0), namespaces=None):
    """Część przypisów (kind: footnote/endnote): separatory o podanych w:id i przypisy {w:id: XML treści}."""
    head = ''.join(
        f'<w:{kind} w:type="{name}" w:id="{number}"><w:p><w:r><w:{name}/></w:r></w:p></w:{kind}>'
        for number, name in zip(separators, ('separator', 'continuationSeparator')))
    body = ''.join(
        f'<w:{kind} w:id="{number}"><w:p><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:{kind}Ref/>'
        f'</w:r>{content}</w:p></w:{kind}>' for number, content in notes.items())
    return f'{XML_HEADER}<w:{kind}s {namespaces or WORD365_NAMESPACES}>{head}{body}</w:{kind}s>'


def note_reference(kind, number, *, custom=None):
    """Odsyłacz przypisu; custom – własny znak (w:customMarkFollows i znak w kolejnym przebiegu)."""
    mark = ' w:customMarkFollows="1"' if custom else ''
    return (f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:{kind}Reference{mark} w:id="{number}"/></w:r>'
            + (r(custom, '<w:vertAlign w:val="superscript"/>') if custom else ''))


def story_part(tag, *paragraphs, namespaces=None):
    """Nagłówek (hdr) albo stopka (ftr) z podanych akapitów."""
    return f'{XML_HEADER}<w:{tag} {namespaces or WORD365_NAMESPACES}>' + ''.join(paragraphs) + f'</w:{tag}>'


def package(parts, *, duplicate=None, stored=False, descriptor=False, zip64=False):
    """ZIP z części {nazwa: tekst lub bajty} (strumień na początku).

    duplicate – nazwa zapisana drugi raz (powielony wpis); stored – bez kompresji; descriptor – deskryptory
    danych za plikami (zapis strumieniowy, bit 3 flag); zip64 – rozszerzenia ZIP64 przy każdym wpisie.
    """
    output = BytesIO()

    class Unseekable:
        """Strumień bez seek: ZipFile dopisuje wtedy deskryptory danych."""

        def write(self, data):
            return output.write(data)

        def flush(self):
            pass

    method = ZIP_STORED if stored else ZIP_DEFLATED
    with ZipFile(Unseekable() if descriptor else output, 'w', method) as archive:
        for name, data in parts.items():
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = method
            with archive.open(info, 'w', force_zip64=zip64) as entry:
                entry.write(data.encode('utf-8') if isinstance(data, str) else data)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)  # celowo powielona nazwa wpisu
                archive.writestr(ZipInfo(duplicate, date_time=(2026, 1, 1, 0, 0, 0)), parts[duplicate])
    output.seek(0)
    return output


def word_parts(body, *, kind='docx', namespaces=WORD365_NAMESPACES, styles=STYLES, section=SECTION, parts=None,
               relationships=(), overrides=(), defaults=(), omit=(), settings='', compat=COMPAT_15, numbering=None,
               document_name='word/document.xml', package_target=None, application='Microsoft Office Word',
               root_attributes=''):
    """Części DOCX zapisanego „jak przez Worda 365” z podaną treścią w:body: {nazwa: tekst lub bajty}.

    kind – typ zawartości części głównej (DOCUMENT_TYPES); parts – dodatkowe części; relationships –
    dodatkowe relacje dokumentu (Id, Type, Target[, TargetMode]); overrides i defaults – dodatkowe wpisy
    [Content_Types] (ścieżka/rozszerzenie, typ); omit – pomijane części (np. 'word/styles.xml');
    settings – elementy w:settings przed w:compat; numbering – treść w:numbering; document_name i
    package_target – nazwa części głównej i jej Target w _rels/.rels (np. '/word/document.xml').
    """
    folder, name = document_name.rsplit('/', 1)
    rels = [
        ('rId1', f'{R_NS}/styles', 'styles.xml'),
        ('rId2', f'{R_NS}/settings', 'settings.xml'),
        ('rId3', f'{R_NS}/fontTable', 'fontTable.xml'),
        ('rId4', f'{R_NS}/theme', 'theme/theme1.xml'),
    ]
    types = [
        ('/word/styles.xml', WORD_PART.format('styles')),
        ('/word/settings.xml', WORD_PART.format('settings')),
        ('/word/fontTable.xml', WORD_PART.format('fontTable')),
        ('/word/theme/theme1.xml', 'application/vnd.openxmlformats-officedocument.theme+xml'),
        (f'/{document_name}', DOCUMENT_TYPES[kind]),
        ('/docProps/app.xml', 'application/vnd.openxmlformats-officedocument.extended-properties+xml'),
    ]
    content = {
        'word/styles.xml': f'{XML_HEADER}<w:styles {namespaces}>{styles}</w:styles>',
        'word/settings.xml': (f'{XML_HEADER}<w:settings {namespaces}><w:zoom w:percent="100"/>'
                              f'<w:defaultTabStop w:val="708"/>{settings}{compat}</w:settings>'),
        'word/fontTable.xml': (f'{XML_HEADER}<w:fonts {namespaces}><w:font w:name="Aptos"/>'
                               '<w:font w:name="Times New Roman"/></w:fonts>'),
        'word/theme/theme1.xml': THEME,
    }
    if numbering is not None:
        rels.append(('rId5', f'{R_NS}/numbering', 'numbering.xml'))
        types.append(('/word/numbering.xml', WORD_PART.format('numbering')))
        content['word/numbering.xml'] = f'{XML_HEADER}<w:numbering {namespaces}>{numbering}</w:numbering>'
    rels += relationships
    types += overrides
    content.update({
        document_name: (f'{XML_HEADER}<w:document {namespaces}{root_attributes}><w:body>{body}{section}'
                        '</w:body></w:document>'),
        f'{folder}/_rels/{name}.rels': _rels([item for item in rels if f'word/{item[2]}' not in omit]),
        '_rels/.rels': _rels([
            ('rId1', f'{R_NS}/officeDocument', package_target or document_name),
            ('rId2', f'{R_NS}/extended-properties', 'docProps/app.xml'),
        ]),
        'docProps/app.xml': ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://'
                             'schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                             f'<Application>{application}</Application></Properties>'),
        **(parts or {}),
    })
    extra = ''.join(f'<Default Extension="{extension}" ContentType="{value}"/>' for extension, value in defaults)
    content['[Content_Types].xml'] = _content_types(
        [item for item in types if item[0].lstrip('/') not in omit], package_rels=False,
    ).replace('</Types>', extra + '</Types>')
    for part_name in omit:
        content.pop(part_name, None)
    return content


def word_docx(body, **options):
    """DOCX „jak z Worda 365” (opcje jak word_parts), strumień na początku."""
    return package(word_parts(body, **options))


# --- Edytory --------------------------------------------------------------------------------------------

EMOJI = ('<w:r><mc:AlternateContent><mc:Choice Requires="w16se"><w16se:symEx w16se:font="Segoe UI Emoji" '
         'w16se:char="1F600"/></mc:Choice><mc:Fallback><w:t>😀</w:t></mc:Fallback></mc:AlternateContent></w:r>')


def word365_docx():
    """Word 365 (Windows, Mac, Online) – znajomość formatu.

    Akapity z ``w14:paraId``/``w14:textId`` i rsid, emoji jako ``w16se:symEx`` w ``mc:AlternateContent``
    (``w:t`` w Fallback), komentarz z częściami ``commentsExtended``/``commentsIds``/``commentsExtensible``/
    ``people`` (``w16cid:durableId``), poprawka z ``w16du:dateUtc``, zakładka ``_GoBack``, ``w:proofErr``,
    ``w:lastRenderedPageBreak``, pole wyboru (``w14:checkbox`` w ``w:sdt``), numer podpisu jako
    ``w:fldSimple`` SEQ (tak zapisuje „Wstaw podpis”), pole DATE i spis treści z galerii, w którym pole TOC
    zaczyna się w pierwszym, a kończy w osobnym, ostatnim akapicie (odsyłacze PAGEREF w hiperłączach).
    """
    ids = 'w14:paraId="1A2B3C4D" w14:textId="77777777" w:rsidR="00A1" w:rsidRDefault="00A1"'
    toc = (
        '<w:sdt><w:sdtPr><w:id w:val="-1146438541"/><w:docPartObj><w:docPartGallery w:val="Table of Contents"/>'
        '<w:docPartUnique/></w:docPartObj></w:sdtPr><w:sdtEndPr/><w:sdtContent>' + p(r('Spis treści'))
        + '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o '
        '"1-3" \\h \\z \\u </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:hyperlink w:anchor="_Toc1" w:history="1">' + r('Rozdział') + '<w:r><w:tab/></w:r>'
        + field('PAGEREF _Toc1 \\h', '1') + '</w:hyperlink></w:p>'
        '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:sdtContent></w:sdt>'
    )
    body = (
        toc
        + f'<w:p {ids}><w:pPr><w:pStyle w:val="Nagwek1"/></w:pPr><w:bookmarkStart w:id="0" w:name="_Toc1"/>'
        + r('Rozdział') + '<w:bookmarkEnd w:id="0"/></w:p>'
        + f'<w:p {ids}><w:commentRangeStart w:id="0"/>' + r('Tekst z komentarzem ')
        + '<w:proofErr w:type="spellStart"/>' + r('błendem') + '<w:proofErr w:type="spellEnd"/>'
        + '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r>' + r(' i emoji ') + EMOJI
        + '<w:ins w:id="1" w:author="Autorka" w:date="2026-01-01T10:00:00Z" w16du:dateUtc="2026-01-01T09:00:00Z">'
        + r(' wstawka') + '</w:ins><w:bookmarkStart w:id="1" w:name="_GoBack"/><w:bookmarkEnd w:id="1"/></w:p>'
        + f'<w:p {ids}><w:r><w:lastRenderedPageBreak/><w:t xml:space="preserve">Pole wyboru </w:t></w:r><w:sdt>'
        '<w:sdtPr><w:id w:val="1"/><w14:checkbox><w14:checked w14:val="0"/><w14:checkedState w14:val="2612" '
        'w14:font="MS Gothic"/><w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox></w:sdtPr>'
        '<w:sdtContent>' + r('☐', '<w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic"/>')
        + '</w:sdtContent></w:sdt>' + r(' do zaznaczenia.') + '</w:p>'
        + p(r('Rysunek '), '<w:fldSimple w:instr=" SEQ Rysunek \\* ARABIC ">' + r('1') + '</w:fldSimple>',
            r('. Podpis.'), properties='<w:pStyle w:val="Legenda"/>')
        + p(r('Data: '), field('DATE \\@ "d MMMM yyyy"', '1 stycznia 2026'), r('.'))
    )
    comment = ('<w:comment w:id="0" w:author="Recenzent" w:date="2026-01-01T10:00:00Z" w:initials="R">'
               '<w:p w14:paraId="0C0C0C0C"><w:r><w:annotationRef/></w:r>' + r('Uwaga.') + '</w:p></w:comment>')
    return word_docx(body, parts={
        'word/comments.xml': f'{XML_HEADER}<w:comments {WORD365_NAMESPACES}>{comment}</w:comments>',
        'word/commentsExtended.xml': (f'{XML_HEADER}<w15:commentsEx {WORD365_NAMESPACES}><w15:commentEx '
                                      'w15:paraId="0C0C0C0C" w15:done="0"/></w15:commentsEx>'),
        'word/commentsIds.xml': (f'{XML_HEADER}<w16cid:commentsIds {WORD365_NAMESPACES}><w16cid:commentId '
                                 'w16cid:paraId="0C0C0C0C" w16cid:durableId="1F2E3D4C"/></w16cid:commentsIds>'),
        'word/commentsExtensible.xml': (f'{XML_HEADER}<w16cex:commentsExtensible {WORD365_NAMESPACES}>'
                                        '<w16cex:commentExtensible w16cex:durableId="1F2E3D4C" '
                                        'w16cex:dateUtc="2026-01-01T09:00:00Z"/></w16cex:commentsExtensible>'),
        'word/people.xml': (f'{XML_HEADER}<w15:people {WORD365_NAMESPACES}><w15:person w15:author="Recenzent">'
                            '<w15:presenceInfo w15:providerId="None" w15:userId="Recenzent"/></w15:person>'
                            '</w15:people>'),
    }, relationships=[
        ('rId10', f'{R_NS}/comments', 'comments.xml'),
        ('rId11', f'{OFFICE}/2011/relationships/commentsExtended', 'commentsExtended.xml'),
        ('rId12', f'{OFFICE}/2016/09/relationships/commentsIds', 'commentsIds.xml'),
        ('rId13', f'{OFFICE}/2018/08/relationships/commentsExtensible', 'commentsExtensible.xml'),
        ('rId14', f'{OFFICE}/2011/relationships/people', 'people.xml'),
    ], overrides=[
        ('/word/comments.xml', WORD_PART.format('comments')),
        ('/word/commentsExtended.xml', WORD_PART.format('commentsExtended')),
        ('/word/commentsIds.xml', WORD_PART.format('commentsIds')),
        ('/word/commentsExtensible.xml', WORD_PART.format('commentsExtensible')),
        ('/word/people.xml', WORD_PART.format('people')),
    ])


WORD2007_NAMESPACES = (
    f'xmlns:w="{W_NS}" xmlns:r="{R_NS}" xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w10="urn:schemas-microsoft-com:office:word" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    f'xmlns:wne="{OFFICE}/word/2006/wordml"'
)


def word2007_docx():
    """Word 2007 i OpenOffice.org 3 (ECMA-376 wyd. 1) – znajomość formatu.

    Bez mc:AlternateContent i przestrzeni w14+; ``w:compat`` ze starymi flagami i bez ``compatibilityMode``
    (Word przyjmuje wtedy 12); pole tekstowe i obraz w VML (``v:shape`` z ``v:textbox``, ``v:imagedata``);
    ``w:smartTag`` (rozpoznane miejsca) i ``w:customXml`` w akapicie oraz wokół akapitu (Word 2007 przed
    usunięciem obsługi custom XML w 2010 r.).
    """
    body = (
        p(r('Mieszkam w '), '<w:smartTag w:uri="urn:schemas-microsoft-com:office:smarttags" w:element="City">'
          '<w:smartTagPr><w:attr w:name="Country" w:val="PL"/></w:smartTagPr>' + r('Warszawie') + '</w:smartTag>',
          r(' i znam '), '<w:customXml w:uri="urn:wydawnictwo" w:element="osoba">' + r('Jana Kowalskiego')
          + '</w:customXml>', r('.'))
        + '<w:customXml w:uri="urn:wydawnictwo" w:element="streszczenie">' + p(r('Akapit w bloku customXml.'))
        + '</w:customXml>'
        + p(r('Pole VML: '), '<w:r><w:pict><v:shape id="Pole1" type="#_x0000_t202" style="position:absolute;'
            'width:120pt;height:30pt"><v:textbox><w:txbxContent>' + p(r('Tekst w polu VML.'))
            + '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>', r(' i obraz VML: '),
            '<w:r><w:pict><v:shape id="Obraz1" type="#_x0000_t75" style="width:20pt;height:10pt">'
            '<v:imagedata r:id="rIdImg" o:title="obraz"/></v:shape></w:pict></w:r>', r(' koniec.'))
    )
    compat = ('<w:compat><w:useFELayout/><w:doNotUseHTMLParagraphAutoSpacing/><w:breakWrappedTables/>'
              '<w:splitPgBreakAndParaMark/></w:compat>')
    return word_docx(body, namespaces=WORD2007_NAMESPACES, compat=compat, parts={'word/media/image1.png': PNG},
                     relationships=[('rIdImg', f'{R_NS}/image', 'media/image1.png')], defaults=[('png', 'image/png')])


STRICT_W = 'http://purl.oclc.org/ooxml/wordprocessingml/main'
STRICT_R = 'http://purl.oclc.org/ooxml/officeDocument/relationships'


def strict_docx():
    """Word „Dokument Strict Open XML” (ISO/IEC 29500 Strict) – znajomość formatu.

    Te same nazwy części i typy zawartości co w zwykłym DOCX, ale przestrzenie nazw i typy relacji
    ``http://purl.oclc.org/ooxml/…`` oraz ``w:conformance="strict"`` – python-docx i Mammoth znają tylko
    przestrzenie Transitional.
    """
    namespaces = f'xmlns:w="{STRICT_W}" xmlns:r="{STRICT_R}"'
    parts = {
        '[Content_Types].xml': _content_types([
            ('/word/document.xml', DOCUMENT_TYPES['docx']), ('/word/styles.xml', WORD_PART.format('styles'))],
            package_rels=False),
        '_rels/.rels': _rels([('rId1', f'{STRICT_R}/officeDocument', 'word/document.xml')]),
        'word/_rels/document.xml.rels': _rels([('rId1', f'{STRICT_R}/styles', 'styles.xml')]),
        'word/styles.xml': (f'{XML_HEADER}<w:styles {namespaces}><w:style w:type="paragraph" w:default="1" '
                            'w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'),
        'word/document.xml': (f'{XML_HEADER}<w:document {namespaces} w:conformance="strict"><w:body>'
                              + p(r('Tekst w formacie Strict.'), properties='<w:jc w:val="start"/>')
                              + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'),
    }
    return package(parts)


WPS_NAMESPACES = WORD365_NAMESPACES.replace(
    'mc:Ignorable="', 'xmlns:wpsCustomData="http://www.wps.cn/officeDocument/2013/wpsCustomData" mc:Ignorable="')


def wps_docx():
    """WPS Office (Kingsoft) – cechy przybliżone ze znajomości eksportu.

    Część ``customXml/item1.xml`` z ``wpsCustomData`` (relacja z ``word/`` przez ``../customXml/…``), liczbowe
    identyfikatory stylów (``1`` – Normal, ``2`` – heading 1), czcionki wschodnioazjatyckie (SimSun,
    ``w:hint="eastAsia"``), ``w:lang w:eastAsia="zh-CN"``, ``w:kern``, ``w:snapToGrid`` i siatka wierszy
    ``w:docGrid w:type="lines"``.
    """
    styles = (
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
        'w:eastAsia="SimSun" w:cs="Times New Roman"/><w:kern w:val="2"/><w:sz w:val="21"/><w:lang w:val="en-US" '
        'w:eastAsia="zh-CN" w:bidi="ar-SA"/></w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="1"><w:name w:val="Normal"/><w:qFormat/><w:pPr>'
        '<w:widowControl w:val="0"/><w:jc w:val="both"/></w:pPr><w:rPr><w:rFonts w:asciiTheme="minorHAnsi" '
        'w:eastAsiaTheme="minorEastAsia"/><w:kern w:val="2"/><w:sz w:val="21"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 1"/><w:basedOn w:val="1"/>'
        '<w:next w:val="1"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="340" w:after="330" w:line="576" '
        'w:lineRule="auto"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:kern w:val="44"/><w:sz w:val="44"/>'
        '</w:rPr></w:style>'
        '<w:style w:type="character" w:default="1" w:styleId="5"><w:name w:val="Default Paragraph Font"/>'
        '<w:semiHidden/></w:style>'
    )
    body = (
        p(r('Rozdział WPS', '<w:rFonts w:hint="eastAsia"/>'), properties='<w:pStyle w:val="2"/>')
        + p(r('Tekst z WPS Office i znaki 中文.', '<w:rFonts w:hint="eastAsia"/><w:lang w:val="pl-PL" '
              'w:eastAsia="zh-CN"/>'), properties='<w:snapToGrid w:val="0"/><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>')
    )
    section = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1800" w:bottom="1440" '
               'w:left="1800" w:header="851" w:footer="992" w:gutter="0"/><w:cols w:space="425" w:num="1"/>'
               '<w:docGrid w:type="lines" w:linePitch="312" w:charSpace="0"/></w:sectPr>')
    custom = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<s:customData xmlns="http://www.wps.cn/'
              'officeDocument/2013/wpsCustomData" xmlns:s="http://www.wps.cn/officeDocument/2013/wpsCustomData">'
              '<customSectProps><customSectPr/></customSectProps><customShpExts/></s:customData>')
    props = (f'{XML_HEADER}<ds:datastoreItem ds:itemID="{{B1977F7D-205B-4081-913C-38D41E755F92}}" '
             'xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"><ds:schemaRefs>'
             '<ds:schemaRef ds:uri="http://www.wps.cn/officeDocument/2013/wpsCustomData"/></ds:schemaRefs>'
             '</ds:datastoreItem>')
    return word_docx(body, namespaces=WPS_NAMESPACES, styles=styles, section=section, application='WPS Office_12.1',
                     settings='<w:bordersDoNotSurroundHeader w:val="1"/><w:characterSpacingControl w:val="compressPunctuation"/>',
                     parts={'customXml/item1.xml': custom, 'customXml/itemProps1.xml': props,
                            'customXml/_rels/item1.xml.rels': _rels([(
                                'rId1', f'{R_NS}/customXmlProps', 'itemProps1.xml')])},
                     relationships=[('rId9', f'{R_NS}/customXml', '../customXml/item1.xml')],
                     overrides=[('/customXml/itemProps1.xml',
                                 'application/vnd.openxmlformats-officedocument.customXmlProperties+xml')])


def onlyoffice_docx():
    """OnlyOffice (Docs, Desktop Editors) – cechy przybliżone ze znajomości eksportu.

    ``docProps/app.xml`` z ``ONLYOFFICE/8.1``, numer strony w stopce jako ``w:fldSimple`` PAGE z
    ``\\* MERGEFORMAT``, hiperłącze z ``w:tooltip`` i ``w:history``, ``w:shd`` z ``w:fill="auto"``,
    ``w:pPrDefault`` z odstępami, ``w:tblLook`` z atrybutami i ``w:sectPr`` z ``w:pgNumType``.
    """
    body = (
        p(r('Akapit z OnlyOffice i '), '<w:hyperlink r:id="rIdLink" w:tooltip="Strona" w:history="1">'
          + r('odnośnikiem', '<w:rStyle w:val="Hyperlink"/>') + '</w:hyperlink>', r('.'),
          properties='<w:shd w:val="clear" w:color="auto" w:fill="auto"/>')
        + '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" '
        'w:firstColumn="1" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr><w:tblGrid><w:gridCol w:w="4500"/>'
        '</w:tblGrid><w:tr><w:tc><w:tcPr><w:tcW w:w="4500" w:type="dxa"/></w:tcPr>' + p(r('Komórka OnlyOffice'))
        + '</w:tc></w:tr></w:tbl>' + p(r('Po tabeli.'))
    )
    section = ('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/><w:pgSz w:w="11906" w:h="16838"/>'
               '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1701" w:header="709" w:footer="709" '
               'w:gutter="0"/><w:pgNumType w:fmt="decimal"/><w:cols w:space="708"/><w:docGrid w:linePitch="360"/>'
               '</w:sectPr>')
    footer = story_part('ftr', p('<w:fldSimple w:instr="PAGE \\* MERGEFORMAT">' + r('1') + '</w:fldSimple>',
                                 properties='<w:jc w:val="center"/>'))
    return word_docx(body, section=section, application='ONLYOFFICE/8.1.0.169', parts={'word/footer1.xml': footer},
                     relationships=[('rIdLink', f'{R_NS}/hyperlink', 'https://example.test/', 'External'),
                                    ('rIdFooter', f'{R_NS}/footer', 'footer1.xml')],
                     overrides=[('/word/footer1.xml', WORD_PART.format('footer'))])


def pages_docx():
    """Apple Pages (Plik → Eksportuj do → Word) – cechy przybliżone ze znajomości eksportu.

    Domyślny styl akapitu ``Body`` (bez ``Normal``), nagłówek w stylu ``Heading`` bez numeru poziomu
    w nazwie, czcionka Helvetica Neue, ``w:docDefaults`` bez języka, strona 11900×16840 i lista punktowana
    z punktorem „•” w Helvetica Neue.
    """
    styles = (
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Helvetica Neue" w:hAnsi="Helvetica Neue" '
        'w:eastAsia="Arial Unicode MS" w:cs="Arial Unicode MS"/><w:sz w:val="22"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Body"><w:name w:val="Body"/><w:pPr>'
        '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr>'
        '<w:color w:val="000000"/><w:sz w:val="22"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading"><w:name w:val="Heading"/><w:next w:val="Body"/>'
        '<w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
    )
    numbering = ('<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
                 '<w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr>'
                 '<w:rPr><w:rFonts w:ascii="Helvetica Neue" w:hAnsi="Helvetica Neue"/></w:rPr></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>')
    body = (
        p(r('Rozdział z Pages'), properties='<w:pStyle w:val="Heading"/>')
        + p(r('Akapit z Pages.'), properties='<w:pStyle w:val="Body"/>')
        + p(r('Punkt listy'), properties='<w:pStyle w:val="Body"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/>'
            '</w:numPr>')
    )
    section = ('<w:sectPr><w:pgSz w:w="11900" w:h="16840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
               'w:left="1440" w:header="720" w:footer="864"/><w:cols w:space="720"/></w:sectPr>')
    return word_docx(body, styles=styles, numbering=numbering, section=section, application='Pages',
                     omit=('word/theme/theme1.xml',))


def google_docs_extras_docx():
    """Google Docs – cechy uzupełniające ``editor_fixtures.google_docs_docx`` (znajomość eksportu).

    Komentarz (``comments.xml``, bez ``commentsExtended``), sugestie jako zmiany śledzone
    (``w:ins``/``w:del`` z autorem), zakładka ``id.…`` celu odnośnika wewnętrznego, obraz pływający
    (``wp:anchor``), tabela, nagłówek strony i spis treści w ``w:sdt`` (pole TOC z hiperłączami do zakładek
    ``_heading=h.…``).
    """
    body = (
        '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/><w:docPartUnique/>'
        '</w:docPartObj></w:sdtPr><w:sdtContent><w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r>'
        '<w:instrText xml:space="preserve">TOC \\h \\u \\z \\t "Heading 1,1,"</w:instrText></w:r><w:r>'
        '<w:fldChar w:fldCharType="separate"/></w:r><w:hyperlink w:anchor="_heading=h.abc">' + r('Rozdział')
        + '</w:hyperlink><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:sdtContent></w:sdt>'
        + p('<w:bookmarkStart w:colFirst="0" w:colLast="0" w:name="_heading=h.abc" w:id="0"/><w:bookmarkEnd w:id="0"/>',
            r('Rozdział'), properties='<w:pStyle w:val="Nagwek1"/>')
        + p('<w:commentRangeStart w:id="0"/>', r('Tekst z komentarzem'), '<w:commentRangeEnd w:id="0"/>',
            '<w:r><w:commentReference w:id="0"/></w:r>', r(' i '),
            '<w:del w:id="1" w:author="Autor sugestii" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>stary'
            '</w:delText></w:r></w:del>',
            '<w:ins w:id="2" w:author="Autor sugestii" w:date="2026-01-01T00:00:00Z">' + r('nowy') + '</w:ins>',
            r(' tekst '), '<w:bookmarkStart w:colFirst="0" w:colLast="0" w:name="id.x1y2z3" w:id="3"/>', r('cel'),
            '<w:bookmarkEnd w:id="3"/>', r('.'))
        + p(r('Obraz: '), drawing(picture('rIdImg'), anchor=True), r(' po obrazie.'))
        + '<w:tbl><w:tblPr><w:tblStyle w:val="Standardowy"/><w:tblW w:w="9029" w:type="dxa"/></w:tblPr><w:tblGrid>'
        '<w:gridCol w:w="4514"/><w:gridCol w:w="4515"/></w:tblGrid><w:tr><w:tc>' + p(r('Lewa'))
        + '</w:tc><w:tc>' + p(r('Prawa')) + '</w:tc></w:tr></w:tbl>' + p(r('Koniec.'))
    )
    section = SECTION.replace('<w:sectPr>', '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>')
    comment = ('<w:comment w:id="0" w:author="Recenzent" w:date="2026-01-01T00:00:00Z" w:initials="">'
               + p(r('Komentarz z Google Docs.')) + '</w:comment>')
    return word_docx(body, section=section, application='', parts={
        'word/comments.xml': f'{XML_HEADER}<w:comments {WORD365_NAMESPACES}>{comment}</w:comments>',
        'word/header1.xml': story_part('hdr', p(r('Nagłówek Google Docs'))),
        'word/media/image1.png': PNG,
    }, relationships=[
        ('rIdComments', f'{R_NS}/comments', 'comments.xml'),
        ('rIdHeader', f'{R_NS}/header', 'header1.xml'),
        ('rIdImg', f'{R_NS}/image', 'media/image1.png'),
    ], overrides=[('/word/comments.xml', WORD_PART.format('comments')),
                  ('/word/header1.xml', WORD_PART.format('header'))], defaults=[('png', 'image/png')])


def minimal_docx():
    """Minimalny eksport (AbiWord, Calligra, generatory skryptowe) – cechy przybliżone.

    Tylko ``[Content_Types].xml``, ``_rels/.rels``, ``word/document.xml`` i pusta lista relacji dokumentu:
    bez ``styles.xml``, ``settings.xml``, ``fontTable.xml``, ``numbering.xml``, motywu i ``docProps``.
    Akapity odwołują się do stylu ``Heading1`` i numeracji, których nie ma, a ostatni nie ma ``w:sectPr``.
    """
    parts = {
        '[Content_Types].xml': _content_types([('/word/document.xml', DOCUMENT_TYPES['docx'])], package_rels=False),
        '_rels/.rels': _rels([('rId1', f'{R_NS}/officeDocument', 'word/document.xml')]),
        'word/_rels/document.xml.rels': _rels([]),
        'word/document.xml': (
            f'{XML_HEADER}<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>'
            + p(r('Tytuł'), properties='<w:pStyle w:val="Heading1"/>')
            + p(r('Punkt'), properties='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
            + p(r('Zwykły akapit.')) + '</w:body></w:document>'),
    }
    return package(parts)


def foreign_file(kind):
    """Plik innego formatu z rozszerzeniem .docx: sygnatury jak w prawdziwych plikach (bajty).

    'doc' – Word 97–2003 (kontener OLE ze strumieniem WordDocument), 'encrypted' – DOCX zabezpieczony
    hasłem (kontener OLE ze strumieniami EncryptionInfo i EncryptedPackage), 'rtf', 'odt' (ZIP z plikiem
    mimetype ODF), 'word2003' (XML Worda 2003 z ``w:wordDocument``), 'html' (strona zapisana jako .docx).
    """
    def ole(*streams):
        header = bytes.fromhex('d0cf11e0a1b11ae1') + b'\x00' * 16 + b'\x3e\x00\x03\x00\xfe\xff\x09\x00'
        entries = b''.join(name.encode('utf-16-le').ljust(64, b'\x00') + b'\x00' * 64
                           for name in ('Root Entry', *streams))
        return header.ljust(512, b'\x00') + entries
    if kind == 'doc':
        return ole('WordDocument', '1Table', '\x05SummaryInformation')
    if kind == 'encrypted':
        return ole('EncryptionInfo', 'EncryptedPackage', '\x06DataSpaces')
    if kind == 'rtf':
        return b'{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Times New Roman;}}\\pard Tekst RTF.\\par}'
    if kind == 'odt':
        return package({'mimetype': 'application/vnd.oasis.opendocument.text',
                        'content.xml': '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:'
                                       'xmlns:office:1.0"/>', 'META-INF/manifest.xml': '<manifest/>'},
                       stored=True).getvalue()
    if kind == 'word2003':
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<?mso-application progid="Word.Document"?>'
                '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml"><w:body><w:p><w:r>'
                '<w:t>Tekst</w:t></w:r></w:p></w:body></w:wordDocument>').encode('utf-8')
    if kind == 'html':
        return '<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head><body><p>Tekst</p></body></html>'.encode()
    raise ValueError(kind)


# --- Elementy problematyczne (znajomość formatu, o ile nie zaznaczono inaczej) ----------------------------

def _header_footer_parts(**stories):
    """Części nagłówków i stopek {rodzaj_wariant: tekst} → (części, relacje, Override, odwołania w:sectPr)."""
    parts, relationships, overrides, references = {}, [], [], ''
    for number, (key, text) in enumerate(stories.items(), 1):
        kind, variant = key.split('_')
        tag = 'hdr' if kind == 'header' else 'ftr'
        name = f'{kind}{number}.xml'
        parts[f'word/{name}'] = story_part(tag, text if text.startswith('<w:') else p(r(text)))
        relationships.append((f'rIdStory{number}', f'{R_NS}/{kind}', name))
        overrides.append((f'/word/{name}', WORD_PART.format(kind)))
        references += f'<w:{kind}Reference w:type="{variant}" r:id="rIdStory{number}"/>'
    return parts, relationships, overrides, references


def complex_fields():
    """Pola złożone: HYPERLINK, PAGEREF, SEQ, DATE z ``w:fldLock`` (jak z LibreOffice), pole zagnieżdżone
    (IF z PAGE), MERGEFIELD, pole bez wyniku, pole niezakończone i wynik pola rozciągnięty na trzy akapity
    (jak spis treści Worda)."""
    nested = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> IF </w:instrText>'
              '</w:r>' + field('PAGE', '2') + '<w:r><w:instrText xml:space="preserve"> &gt; 1 "dalej" "" </w:instrText>'
              '</w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>' + r('dalej')
              + '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    return word_docx(
        p(r('Zobacz '), field('HYPERLINK "https://example.test/"', 'stronę wydawnictwa'), r(', s. '),
          field('PAGEREF _Ref1 \\h', '3'), r(', tabela '), field('SEQ Tabela \\* ARABIC', '2'), r(', dnia '),
          field('DATE \\@ "d.MM.yyyy"', '1.01.2026', lock=True), r(' i '), nested, r('.'))
        + p(r('Drogi '), field('MERGEFIELD Imię', '«Imię»'), r(' i pole bez wyniku: '),
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>', r('koniec.'))
        + '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" '
        '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>' + r('Początek wyniku') + '</w:p>'
        + p(r('Środek wyniku')) + p(r('Koniec wyniku'), '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        + p(r('Niezakończone '), '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> REF x '
            '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>', r('wynik bez końca.'))
        + p(r('Ostatni akapit.')))


def simple_fields():
    """Pola proste (w:fldSimple) w treści – numer podpisu SEQ, MERGEFIELD, pole z w:fldData, pole w polu –
    i numer strony (PAGE, NUMPAGES) jako pole proste w stopce, jak w OnlyOffice i Google Docs."""
    parts, relationships, overrides, references = _header_footer_parts(
        footer_default=p(r('Strona '), '<w:fldSimple w:instr="PAGE">' + r('1') + '</w:fldSimple>', r(' z '),
                         '<w:fldSimple w:instr="NUMPAGES">' + r('9') + '</w:fldSimple>'))
    return word_docx(
        p(r('Tabela '), '<w:fldSimple w:instr=" SEQ Tabela \\* ARABIC ">' + r('4') + '</w:fldSimple>',
          r('. Podpis tabeli.'))
        + p(r('Szanowny '), '<w:fldSimple w:instr=" MERGEFIELD Nazwisko "><w:fldData xml:space="preserve">AAAA'
            '</w:fldData>' + r('«Nazwisko»') + '</w:fldSimple>', r(','))
        + p('<w:fldSimple w:instr=" IF 1 = 1 ">' + r('zewnętrzne ') + '<w:fldSimple w:instr=" PAGE ">' + r('7')
            + '</w:fldSimple></w:fldSimple>', r(' po polu.')),
        section=SECTION.replace('<w:sectPr>', '<w:sectPr>' + references), parts=parts,
        relationships=relationships, overrides=overrides)


def content_controls():
    """Kontrolki zawartości (w:sdt): blokowa z dwoma akapitami, w wierszu i komórce tabeli, data, lista
    rozwijana (jak z LibreOffice), tekst zastępczy, powtarzana sekcja (w15) z powiązaniem danych i kontrolka
    w nagłówku strony."""
    parts, relationships, overrides, references = _header_footer_parts(
        header_default='<w:sdt><w:sdtPr><w:alias w:val="Tytuł"/><w:dataBinding w:xpath="/ns0:coreProperties[1]/'
                       'ns1:title[1]" w:storeItemID="{6C3C8BC8-F283-45AE-878A-BAB7291924A1}"/></w:sdtPr>'
                       '<w:sdtContent>' + p(r('Tytuł w nagłówku')) + '</w:sdtContent></w:sdt>')
    row = ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="4000"/>'
           '<w:gridCol w:w="4000"/></w:tblGrid><w:sdt><w:sdtPr><w:id w:val="5"/></w:sdtPr><w:sdtContent><w:tr>'
           '<w:sdt><w:sdtPr><w:id w:val="6"/></w:sdtPr><w:sdtContent><w:tc>' + p(r('Komórka w kontrolce'))
           + '</w:tc></w:sdtContent></w:sdt><w:tc>' + p(r('Zwykła komórka')) + '</w:tc></w:tr></w:sdtContent>'
           '</w:sdt></w:tbl>')
    return word_docx(
        '<w:sdt><w:sdtPr><w:id w:val="1"/><w:richText/></w:sdtPr><w:sdtContent>' + p(r('Pierwszy w bloku'))
        + p(r('Drugi w bloku')) + '</w:sdtContent></w:sdt>' + row
        + p(r('Data: '), '<w:sdt><w:sdtPr><w:id w:val="2"/><w:date w:fullDate="2026-01-01T00:00:00Z"><w:dateFormat '
            'w:val="d.MM.yyyy"/><w:lid w:val="pl-PL"/></w:date></w:sdtPr><w:sdtContent>' + r('1.01.2026')
            + '</w:sdtContent></w:sdt>', r(', wybór: '), '<w:sdt><w:sdtPr><w:alias w:val="Wybór"/><w:dropDownList '
            'w:lastValue="0"><w:listItem w:value="Opcja A" w:displayText="Opcja A"/></w:dropDownList></w:sdtPr>'
            '<w:sdtContent>' + r('Opcja A') + '</w:sdtContent></w:sdt>', r(', puste: '),
            '<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>'
            + r('Kliknij tutaj, aby wprowadzić tekst.', '<w:rStyle w:val="Tekstzastpczy"/>')
            + '</w:sdtContent></w:sdt>', r('.'))
        + '<w:sdt><w:sdtPr><w15:repeatingSection/></w:sdtPr><w:sdtContent><w:sdt><w:sdtPr><w15:repeatingSectionItem/>'
        '</w:sdtPr><w:sdtContent>' + p(r('Element powtarzany')) + '</w:sdtContent></w:sdt></w:sdtContent></w:sdt>'
        + p(r('Po kontrolkach.')),
        section=SECTION.replace('<w:sectPr>', '<w:sectPr>' + references), parts=parts,
        relationships=relationships, overrides=overrides)


def alt_chunk():
    """Dołączona treść zewnętrzna (w:altChunk) w HTML i RTF – Word wstawia ją przy otwarciu."""
    return word_docx(
        p(r('Przed wstawką.')) + '<w:altChunk r:id="rIdHtml"/><w:altChunk r:id="rIdRtf"/>' + p(r('Po wstawce.')),
        parts={'word/afchunk.htm': '<html><body><p>Treść HTML wstawki.</p></body></html>',
               'word/afchunk.rtf': '{\\rtf1\\ansi Treść RTF wstawki.\\par}'},
        relationships=[('rIdHtml', f'{R_NS}/aFChunk', 'afchunk.htm'), ('rIdRtf', f'{R_NS}/aFChunk', 'afchunk.rtf')],
        defaults=[('htm', 'text/html'), ('rtf', 'application/rtf')])


def tracked_changes():
    """Zmiany śledzone: wstawka, usunięcie, przeniesienie (moveFrom/moveTo z zakresami), zmiana formatowania
    znaków i akapitu, wstawiony i usunięty znak akapitu, wstawka usunięta przez drugą osobę, wstawiona
    komórka i zmiana właściwości tabeli – także jak sugestie z Google Docs."""
    meta = 'w:author="Redaktor" w:date="2026-01-01T10:00:00Z"'
    return word_docx(
        p(r('Tekst '), f'<w:ins w:id="1" {meta}>' + r('wstawiony') + '</w:ins>', r(' i '),
          f'<w:del w:id="2" {meta}><w:r><w:delText>usunięty</w:delText></w:r></w:del>', r(' oraz '),
          f'<w:ins w:id="3" {meta}><w:del w:id="4" w:author="Autorka" w:date="2026-01-02T10:00:00Z"><w:r>'
          '<w:delText>cofnięty</w:delText></w:r></w:del></w:ins>',
          r('zmieniony', f'<w:b/><w:rPrChange w:id="5" {meta}><w:rPr/></w:rPrChange>'), r('.'),
          properties=f'<w:jc w:val="center"/><w:pPrChange w:id="6" {meta}><w:pPr/></w:pPrChange>')
        + f'<w:moveFromRangeStart w:id="7" {meta} w:name="move1"/>'
        + p(f'<w:moveFrom w:id="8" {meta}>' + r('Przeniesiony akapit.') + '</w:moveFrom>')
        + '<w:moveFromRangeEnd w:id="7"/>'
        + p(r('Akapit z nowym znakiem końca'), properties=f'<w:rPr><w:ins w:id="9" {meta}/></w:rPr>')
        + p(r('i jego dalszy ciąg, '), properties=f'<w:rPr><w:del w:id="10" {meta}/></w:rPr>')
        + p(r('połączony po akceptacji.'))
        + f'<w:moveToRangeStart w:id="11" {meta} w:name="move1"/>'
        + p(f'<w:moveTo w:id="12" {meta}>' + r('Przeniesiony akapit.') + '</w:moveTo>')
        + '<w:moveToRangeEnd w:id="11"/>'
        + f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblPrChange w:id="13" {meta}><w:tblPr/></w:tblPrChange>'
        '</w:tblPr><w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc><w:tcPr>'
        f'<w:cellIns w:id="14" {meta}/></w:tcPr>' + p(r('Nowa komórka')) + '</w:tc></w:tr></w:tbl>'
        + p(f'<w:del w:id="15" {meta}><w:r><w:delText>Cały usunięty akapit.</w:delText></w:r></w:del>',
            properties=f'<w:rPr><w:del w:id="16" {meta}/></w:rPr>')
        + p(r('Koniec.')))


def _comment_parts(comments, *, namespaces=WORD365_NAMESPACES, extended=None, word365=True):
    """Części komentarzy {nazwa: XML}, relacje i Override: comments.xml z podanych w:comment, a przy word365 także
    commentsExtended (extended: {paraId: (paraIdParent, done)}), commentsIds, commentsExtensible i people."""
    parts = {'word/comments.xml': f'{XML_HEADER}<w:comments {namespaces}>{comments}</w:comments>'}
    relationships = [('rIdComments', f'{R_NS}/comments', 'comments.xml')]
    overrides = [('/word/comments.xml', WORD_PART.format('comments'))]
    if extended is not None:
        entries = ''.join(f'<w15:commentEx w15:paraId="{para}"'
                          + (f' w15:paraIdParent="{parent}"' if parent else '') + f' w15:done="{int(done)}"/>'
                          for para, (parent, done) in extended.items())
        parts['word/commentsExtended.xml'] = f'{XML_HEADER}<w15:commentsEx {namespaces}>{entries}</w15:commentsEx>'
        relationships.append(('rIdCommentsEx', f'{OFFICE}/2011/relationships/commentsExtended', 'commentsExtended.xml'))
        overrides.append(('/word/commentsExtended.xml', WORD_PART.format('commentsExtended')))
    if word365:
        durable = {para: f'{0x1A000000 + number:08X}' for number, para in enumerate(extended or {}, 1)}
        parts['word/commentsIds.xml'] = (f'{XML_HEADER}<w16cid:commentsIds {namespaces}>' + ''.join(
            f'<w16cid:commentId w16cid:paraId="{para}" w16cid:durableId="{value}"/>' for para, value in durable.items())
            + '</w16cid:commentsIds>')
        parts['word/commentsExtensible.xml'] = (f'{XML_HEADER}<w16cex:commentsExtensible {namespaces}>' + ''.join(
            f'<w16cex:commentExtensible w16cex:durableId="{value}" w16cex:dateUtc="2026-02-01T09:00:00Z"/>'
            for value in durable.values()) + '</w16cex:commentsExtensible>')
        parts['word/people.xml'] = (
            f'{XML_HEADER}<w15:people {namespaces}>' + ''.join(
                f'<w15:person w15:author="{author}"><w15:presenceInfo w15:providerId="AD" '
                f'w15:userId="S::{login}@wydawnictwo.test::0000"/></w15:person>'
                for author, login in (('Recenzentka', 'recenzentka'), ('Autorka', 'autorka'))) + '</w15:people>')
        for name, kind, relationship in (
                ('commentsIds', 'commentsIds', f'{OFFICE}/2016/09/relationships/commentsIds'),
                ('commentsExtensible', 'commentsExtensible', f'{OFFICE}/2018/08/relationships/commentsExtensible'),
                ('people', 'people', f'{OFFICE}/2011/relationships/people')):
            relationships.append((f'rId{name}', relationship, f'{name}.xml'))
            overrides.append((f'/word/{name}.xml', WORD_PART.format(kind)))
    return parts, relationships, overrides


def _comment(number, author, *paragraphs, para, initials='R', date='2026-02-01T10:00:00Z'):
    """Komentarz Worda: akapity treści w stylu tekstu komentarza, w pierwszym znacznik (w:annotationRef); para –
    w14:paraId pierwszego akapitu, kolejne mają następne numery (paraId ostatniego wiąże z commentsExtended)."""
    mark = '<w:r><w:rPr><w:rStyle w:val="Odwoaniedokomentarza"/></w:rPr><w:annotationRef/></w:r>'
    return (f'<w:comment w:id="{number}" w:author="{author}" w:date="{date}" w:initials="{initials}">' + ''.join(
        f'<w:p w14:paraId="{para + index:08X}" w14:textId="77777777"><w:pPr><w:pStyle w:val="Tekstkomentarza"/>'
        f'</w:pPr>{"" if index else mark}{content}</w:p>' for index, content in enumerate(paragraphs))
        + '</w:comment>')


def _comment_reference(number):
    return f'<w:r><w:rPr><w:rStyle w:val="Odwoaniedokomentarza"/></w:rPr><w:commentReference w:id="{number}"/></w:r>'


# Style komentarzy polskiego Worda (nazwy wbudowane po angielsku).
REVIEW_STYLES = STYLES + (
    '<w:style w:type="paragraph" w:styleId="Tekstkomentarza"><w:name w:val="annotation text"/>'
    '<w:basedOn w:val="Normalny"/><w:rPr><w:sz w:val="20"/></w:rPr></w:style>'
    '<w:style w:type="character" w:styleId="Odwoaniedokomentarza"><w:name w:val="annotation reference"/>'
    '<w:rPr><w:sz w:val="16"/></w:rPr></w:style>')


def review_docx():
    """Recenzja w Wordzie 365: zmiany śledzone i komentarze – znajomość formatu (ECMA-376, [MS-DOCX]).

    Zmiany: wstawka, usunięcie, usunięcie cudzej wstawki (``w:ins`` z ``w:del`` innej osoby), przeniesienie akapitu
    (``moveFrom``/``moveTo`` z nazwanymi zakresami między akapitami, tekst przeniesiony jako ``w:t``), wstawiony
    i usunięty znak akapitu, akapit usunięty w całości, zmiana formatowania znaków (pogrubienie zamiast kursywy,
    ``w:rPrChange``) i akapitu (wyśrodkowanie, ``w:pPrChange``), poprawka z ``w16du:dateUtc`` i wstawka w przypisie.
    Komentarze: z zakresem na dwa akapity (treść w dwóch akapitach z pogrubieniem i kursywą), punktowy (bez
    zakresu), z odpowiedzią innej osoby, rozwiązany i w przypisie; części ``commentsExtended`` (odpowiedź,
    rozwiązanie), ``commentsIds``, ``commentsExtensible`` i ``people`` (identyfikatory kont autorów).
    """
    a = 'w:author="Recenzentka" w:date="2026-02-01T10:00:00Z"'
    b = 'w:author="Autorka" w:date="2026-02-02T11:30:00Z"'
    body = (
        p('<w:commentRangeStart w:id="0"/>', r('Początek komentarza, '), f'<w:ins w:id="10" {a}>' + r('wstawiony')
          + '</w:ins>', r(' i '), f'<w:del w:id="11" {a}><w:r><w:delText>usunięty</w:delText></w:r></w:del>',
          r(' oraz '), f'<w:ins w:id="12" {b} w16du:dateUtc="2026-02-02T10:30:00Z"><w:del w:id="13" {a}><w:r>'
          '<w:delText>cofnięty</w:delText></w:r></w:del></w:ins>', r(' tekst.'),
          properties=f'<w:jc w:val="center"/><w:pPrChange w:id="14" {a}><w:pPr/></w:pPrChange>')
        + p(r('Dalszy ciąg '), r('pogrubiony', f'<w:b/><w:rPrChange w:id="15" {a}><w:rPr><w:i/></w:rPr></w:rPrChange>'),
            r(' i koniec komentarza.'), '<w:commentRangeEnd w:id="0"/>', _comment_reference(0))
        + p(r('Punkt'), _comment_reference(1), r(' bez zakresu i przypis'), note_reference('footnote', 1), r('.'))
        + f'<w:moveFromRangeStart w:id="16" {a} w:name="move1"/>'
        + p(f'<w:moveFrom w:id="17" {a}>' + r('Przeniesiony akapit.') + '</w:moveFrom>',
            properties=f'<w:rPr><w:moveFrom w:id="18" {a}/></w:rPr>')
        + '<w:moveFromRangeEnd w:id="16"/>'
        + p(r('Akapit z nowym znakiem końca'), properties=f'<w:rPr><w:ins w:id="19" {a}/></w:rPr>')
        + p(r('i dalszy ciąg, '), properties=f'<w:rPr><w:del w:id="20" {a}/></w:rPr>')
        + p('<w:commentRangeStart w:id="2"/><w:commentRangeStart w:id="3"/>', r('połączony po przyjęciu'),
            '<w:commentRangeEnd w:id="2"/>', _comment_reference(2), '<w:commentRangeEnd w:id="3"/>',
            _comment_reference(3), r('.'))
        + f'<w:moveToRangeStart w:id="21" {a} w:name="move1"/>'
        + p(f'<w:moveTo w:id="22" {a}>' + r('Przeniesiony akapit.') + '</w:moveTo>',
            properties=f'<w:rPr><w:moveTo w:id="23" {a}/></w:rPr>')
        + '<w:moveToRangeEnd w:id="21"/>'
        + p('<w:commentRangeStart w:id="4"/>', r('Rozwiązany'), '<w:commentRangeEnd w:id="4"/>', _comment_reference(4),
            r(' komentarz.'))
        + p(f'<w:del w:id="24" {a}><w:r><w:delText>Cały usunięty akapit.</w:delText></w:r></w:del>',
            properties=f'<w:rPr><w:del w:id="25" {a}/></w:rPr>')
        + p(r('Koniec.')))
    footnotes = notes_part('footnote', {1: r(' Przypis z ') + '<w:commentRangeStart w:id="5"/>' + r('uwagą')
                                        + '<w:commentRangeEnd w:id="5"/>' + _comment_reference(5)
                                        + f'<w:ins w:id="26" {b}>' + r(' i wstawką') + '</w:ins>' + r('.')})
    comments = (
        _comment(0, 'Recenzentka', r('Uwaga do ') + r('dwóch', '<w:b/>') + r(' akapitów.'),
                 r('Drugi akapit ', '<w:i/>') + r('komentarza.'), para=0x0A000001)
        + _comment(1, 'Redaktor', r('Komentarz punktowy.'), para=0x0B000001, initials='Red')
        + _comment(2, 'Recenzentka', r('Pytanie?'), para=0x0C000001)
        + _comment(3, 'Autorka', r('Odpowiedź.'), para=0x0D000001, initials='A', date='2026-02-02T11:30:00Z')
        + _comment(4, 'Recenzentka', r('Rozwiązane.'), para=0x0E000001)
        + _comment(5, 'Recenzentka', r('W przypisie.'), para=0x0F000001))
    parts, relationships, overrides = _comment_parts(comments, extended={
        '0A000002': (None, False), '0B000001': (None, False), '0C000001': (None, False),
        '0D000001': ('0C000001', False), '0E000001': (None, True), '0F000001': (None, False)})
    parts['word/footnotes.xml'] = footnotes
    return word_docx(body, styles=REVIEW_STYLES, parts=parts,
                     relationships=[*relationships, ('rIdFoot', f'{R_NS}/footnotes', 'footnotes.xml')],
                     overrides=[*overrides, ('/word/footnotes.xml', WORD_PART.format('footnotes'))],
                     settings='<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>')


def review_stories_docx():
    """Dwa opowiadania (Nagłówek 1) ze zmianami i komentarzami na granicy opowiadań – znajomość formatu.

    Komentarz 0 zaczyna się w pierwszym opowiadaniu, a kończy (z odwołaniem) w drugim; akapit przeniesiony
    z pierwszego do drugiego (moveFrom/moveTo z zakresami „move1”); w pierwszym wstawka i komentarz punktowy 1,
    w drugim komentarz 2 z odpowiedzią 3 (ten sam zakres, commentsExtended) i komentarz 4 w przypisie.
    """
    a = 'w:author="Recenzentka" w:date="2026-02-01T10:00:00Z"'
    heading = '<w:pStyle w:val="Nagwek1"/>'
    body = (
        p(r('Pierwsze'), properties=heading)
        + p('<w:commentRangeStart w:id="0"/>', r('Początek '), f'<w:ins w:id="10" {a}>' + r('wstawki') + '</w:ins>',
            r(' i punkt'), _comment_reference(1), r('.'))
        + f'<w:moveFromRangeStart w:id="11" {a} w:name="move1"/>'
        + p(f'<w:moveFrom w:id="12" {a}>' + r('Przeniesiony akapit.') + '</w:moveFrom>',
            properties=f'<w:rPr><w:moveFrom w:id="13" {a}/></w:rPr>')
        + '<w:moveFromRangeEnd w:id="11"/>'
        + p(r('Drugie'), properties=heading)
        + p(r('Koniec komentarza'), '<w:commentRangeEnd w:id="0"/>', _comment_reference(0), r(' i przypis'),
            note_reference('footnote', 1), r('.'))
        + f'<w:moveToRangeStart w:id="14" {a} w:name="move1"/>'
        + p(f'<w:moveTo w:id="15" {a}>' + r('Przeniesiony akapit.') + '</w:moveTo>',
            properties=f'<w:rPr><w:moveTo w:id="16" {a}/></w:rPr>')
        + '<w:moveToRangeEnd w:id="14"/>'
        + p('<w:commentRangeStart w:id="2"/><w:commentRangeStart w:id="3"/>', r('Pytanie'),
            '<w:commentRangeEnd w:id="2"/>', _comment_reference(2), '<w:commentRangeEnd w:id="3"/>',
            _comment_reference(3), r('.')))
    comments = (_comment(0, 'Recenzentka', r('Przez granicę.'), para=0x0A000001)
                + _comment(1, 'Redaktor', r('Punktowy.'), para=0x0B000001, initials='Red')
                + _comment(2, 'Recenzentka', r('Pytanie?'), para=0x0C000001)
                + _comment(3, 'Autorka', r('Odpowiedź.'), para=0x0D000001, initials='A')
                + _comment(4, 'Recenzentka', r('W przypisie.'), para=0x0E000001))
    parts, relationships, overrides = _comment_parts(comments, extended={
        '0A000001': (None, False), '0B000001': (None, False), '0C000001': (None, False),
        '0D000001': ('0C000001', False), '0E000001': (None, True)})
    parts['word/footnotes.xml'] = notes_part('footnote', {1: r(' Przypis z ') + '<w:commentRangeStart w:id="4"/>'
                                                         + r('uwagą') + '<w:commentRangeEnd w:id="4"/>'
                                                         + _comment_reference(4)})
    return word_docx(body, styles=REVIEW_STYLES, parts=parts,
                     relationships=[*relationships, ('rIdFoot', f'{R_NS}/footnotes', 'footnotes.xml')],
                     overrides=[*overrides, ('/word/footnotes.xml', WORD_PART.format('footnotes'))],
                     settings='<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>')


def libreoffice_review_docx():
    """Recenzja zapisana przez LibreOffice 24.2.7 („Word 2007–365”) – cechy z próbki: review_docx bez komentarza
    w przypisie (takiego pliku LibreOffice 24.2 nie otwiera) otwarty i zapisany ponownie przez soffice.

    Identyfikatory w:id liczone osobno dla każdego rodzaju (wstawka, zmiana formatowania, zakres przeniesienia
    i komentarz mają w:id 0); pusty ``w:pPrChange`` (bez ``w:pPr``) przed ``w:rPr`` w ``w:pPr`` (niezgodnie ze
    schematem); zakresy przeniesień wewnątrz ``w:moveFrom``/``w:moveTo``, a ich koniec za nimi w akapicie; każdy
    przebieg z pustym ``w:rPr``, puste przebiegi przy odwołaniach do komentarzy, odwołanie bez ``w:rPr``;
    komentarze bez znacznika ``w:annotationRef`` i stylu, z pełnym ``w:rPr`` (Liberation Serif), ``w14:paraId``
    tylko w wątkach i rozwiązanych; ``commentsExtended`` tylko z odpowiedzią (``w15:paraIdParent``) i rozwiązaniem
    (``w15:done="1"``, bez ``w15:done="0"``), komentarz nadrzędny o wyższym w:id niż odpowiedź.
    """
    a = 'w:author="Recenzentka" w:date="2026-02-01T10:00:00Z"'
    b = 'w:author="Autorka" w:date="2026-02-02T11:30:00Z"'

    def lo(*runs, properties=''):
        return p(*runs, properties='<w:pStyle w:val="Normal"/>' + properties + '<w:rPr></w:rPr>')

    def run(text, properties=''):
        return r(text, properties or ' ').replace('<w:rPr> </w:rPr>', '<w:rPr></w:rPr>')

    def reference(number):
        return f'<w:r><w:commentReference w:id="{number}"/></w:r>'

    def mark(revision):
        return f'<w:pStyle w:val="Normal"/><w:rPr>{revision}</w:rPr>'

    body = (
        '<w:p><w:pPr><w:pStyle w:val="Normal"/><w:jc w:val="center"/>'
        f'<w:pPrChange w:id="0" {a}></w:pPrChange><w:rPr></w:rPr></w:pPr><w:commentRangeStart w:id="0"/>'
        + run('Początek komentarza, ') + f'<w:ins w:id="0" {a}>' + run('wstawiony') + '</w:ins>' + run(' i ')
        + f'<w:del w:id="1" {a}><w:r><w:rPr></w:rPr><w:delText>usunięty</w:delText></w:r></w:del>' + run(' oraz ')
        + f'<w:ins w:id="2" {b}><w:del w:id="3" {a}><w:r><w:rPr></w:rPr><w:delText>cofnięty</w:delText></w:r></w:del>'
        '</w:ins>' + run(' tekst.') + '</w:p>'
        + lo(run('Dalszy ciąg '), run('pogrubiony', f'<w:b/><w:rPrChange w:id="0" {a}><w:rPr><w:i/></w:rPr>'
                                                     '</w:rPrChange>'), run(' i koniec komentarza.'),
             '<w:commentRangeEnd w:id="0"/>', reference(0), '<w:r><w:rPr></w:rPr></w:r>')
        + lo(run('Punkt'), reference(1), run(' bez zakresu.'))
        + p(f'<w:moveFrom w:id="5" {a}><w:moveFromRangeStart w:id="0" {a} w:name="move1"/>'
            + run('Przeniesiony akapit.') + '</w:moveFrom><w:moveFromRangeEnd w:id="0"/>',
            properties=mark(f'<w:moveFrom w:id="6" {a}></w:moveFrom>'))
        + p(run('Akapit z nowym znakiem końca'), properties=mark(f'<w:ins w:id="7" {a}></w:ins>'))
        + p(run('i dalszy ciąg, '), properties=mark(f'<w:del w:id="8" {a}></w:del>'))
        + lo('<w:commentRangeStart w:id="3"/><w:commentRangeStart w:id="2"/>', run('połączony po przyjęciu'),
             '<w:r><w:rPr></w:rPr></w:r><w:commentRangeEnd w:id="2"/>', reference(2), '<w:r><w:rPr></w:rPr></w:r>',
             '<w:commentRangeEnd w:id="3"/>', reference(3), run('.'))
        + p(f'<w:moveTo w:id="9" {a}><w:moveToRangeStart w:id="1" {a} w:name="move1"/>'
            + run('Przeniesiony akapit.') + '</w:moveTo><w:moveToRangeEnd w:id="1"/>',
            properties=mark(f'<w:moveTo w:id="10" {a}></w:moveTo>'))
        + lo('<w:commentRangeStart w:id="4"/>', run('Rozwiązany'), '<w:r><w:rPr></w:rPr></w:r>',
             '<w:commentRangeEnd w:id="4"/>', reference(4), run(' komentarz.'))
        + p(f'<w:del w:id="11" {a}><w:r><w:rPr></w:rPr><w:delText>Cały usunięty akapit.</w:delText></w:r></w:del>',
            properties=mark(f'<w:del w:id="12" {a}></w:del>'))
        + lo(run('Koniec.')))
    font = ('<w:rFonts w:ascii="Liberation Serif" w:hAnsi="Liberation Serif" w:eastAsia="DejaVu Sans" '
            'w:cs="DejaVu Sans"/>{}<w:szCs w:val="24"/><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="en-US"/>')

    def comment(number, author, *paragraphs, para=None, initials='R', date='2026-02-01T10:00:00Z'):
        identity = f' w14:paraId="{para}"' if para else ''
        return (f'<w:comment w:id="{number}" w:author="{author}" w:date="{date}" w:initials="{initials}">' + ''.join(
            f'<w:p{identity if index == len(paragraphs) - 1 else ""}><w:pPr><w:overflowPunct w:val="false"/>'
            '<w:rPr></w:rPr></w:pPr>' + ''.join(r(text, font.format(extra)) for text, extra in runs) + '</w:p>'
            for index, runs in enumerate(paragraphs)) + '</w:comment>')

    comments = (
        comment(0, 'Recenzentka', [('Uwaga do ', ''), ('dwóch', '<w:b/>'), (' akapitów.', '')],
                [('Drugi akapit ', '<w:i/>'), ('komentarza.', '')])
        + comment(1, 'Redaktor', [('Komentarz punktowy.', '')], initials='Red')
        + comment(3, 'Recenzentka', [('Pytanie?', '')], para='01000000')
        + comment(2, 'Autorka', [('Odpowiedź.', '')], para='02000000', initials='A', date='2026-02-02T11:30:00Z')
        + comment(4, 'Recenzentka', [('Rozwiązane.', '')], para='03000000'))
    namespaces = WORD365_NAMESPACES.replace(' mc:Ignorable="w14 w15 w16se w16cid w16 w16cex w16sdtdh w16sdtfl w16du '
                                            'w16xyz wp14"', ' mc:Ignorable="w14"')
    parts, relationships, overrides = _comment_parts(
        comments, namespaces=namespaces, word365=False,
        extended={'02000000': ('01000000', False), '03000000': (None, True)})
    parts['word/commentsExtended.xml'] = parts['word/commentsExtended.xml'].replace(' w15:done="0"', '')
    return word_docx(body, styles=STYLES + '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/>'
                                           '</w:style>', parts=parts, relationships=relationships,
                     overrides=overrides, application='LibreOffice/24.2.7.2$Linux_X86_64 LibreOffice_project/420$Build-2')


def unusual_notes():
    """Przypisy o nietypowych w:id: separatory 0 i 1 (LibreOffice), przypisy 5 i 17 (luki, jak pandoc
    z 20 i 21), przypis z własnym znakiem „*”, odsyłacz do separatora (w:id 0 – błąd generatora),
    przypis typu continuationNotice i przypis końcowy z tabelą."""
    footnotes = notes_part('footnote', {5: r(' Pierwszy przypis.'), 17: r(' Przypis z gwiazdką.')},
                           separators=(0, 1)).replace(
        '</w:footnotes>', '<w:footnote w:type="continuationNotice" w:id="2"><w:p>' + r('(ciąg dalszy)')
        + '</w:p></w:footnote></w:footnotes>')
    endnotes = notes_part('endnote', {3: r(' Przypis końcowy.')}).replace(
        '</w:p></w:endnote></w:endnotes>', '</w:p><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>'
        '<w:tr><w:tc>' + p(r('Tabela w przypisie')) + '</w:tc></w:tr></w:tbl></w:endnote></w:endnotes>')
    return word_docx(
        p(r('Tekst'), note_reference('footnote', 5), r(' dalej'), note_reference('footnote', 17, custom='*'),
          r(' i błędny'), note_reference('footnote', 0), r(' oraz końcowy'), note_reference('endnote', 3), r('.')),
        parts={'word/footnotes.xml': footnotes, 'word/endnotes.xml': endnotes},
        relationships=[('rIdFoot', f'{R_NS}/footnotes', 'footnotes.xml'),
                       ('rIdEnd', f'{R_NS}/endnotes', 'endnotes.xml')],
        overrides=[('/word/footnotes.xml', WORD_PART.format('footnotes')),
                   ('/word/endnotes.xml', WORD_PART.format('endnotes'))],
        settings='<w:footnotePr><w:footnote w:id="0"/><w:footnote w:id="1"/></w:footnotePr>')


def frames_and_drop_cap():
    """Inicjał Worda (Wstawianie → Inicjał): pierwsza litera jest osobnym akapitem z ``w:framePr
    w:dropCap="drop"``, a reszta słowa zaczyna następny akapit; do tego zwykła ramka akapitu (w:framePr)."""
    return word_docx(
        p(r('W', '<w:position w:val="-7"/><w:sz w:val="84"/>'),
          properties='<w:keepNext/><w:framePr w:dropCap="drop" w:lines="3" w:wrap="around" w:vAnchor="text" '
                     'w:hAnchor="text"/><w:spacing w:line="827" w:lineRule="exact"/><w:textAlignment w:val="baseline"/>')
        + p(r('ielki inicjał zaczyna ten akapit.'))
        + p(r('Tekst w ramce akapitu.'), properties='<w:framePr w:w="3000" w:h="600" w:hSpace="180" w:wrap="around" '
            'w:vAnchor="text" w:hAnchor="page" w:x="2000" w:y="1"/>')
        + p(r('Zwykły akapit.')))


def sections_and_columns():
    """Sekcje i kolumny: sekcja pozioma A4 w dwóch kolumnach (podział ciągły), potem sekcja z innymi
    nagłówkami pierwszej i parzystej strony (w:titlePg, w:evenAndOddHeaders) i stopką z w:ptab."""
    parts, relationships, overrides, references = _header_footer_parts(
        header_default='Nagłówek nieparzysty', header_even='Nagłówek parzysty', header_first='Pierwsza strona',
        footer_default=p(r('Autor'), '<w:r><w:ptab w:relativeTo="margin" w:alignment="right" w:leader="none"/></w:r>',
                         r('Tytuł')))
    landscape = ('<w:sectPr><w:type w:val="continuous"/><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
                 '<w:pgMar w:top="1417" w:right="1417" w:bottom="1417" w:left="1417" w:header="708" w:footer="708" '
                 'w:gutter="0"/><w:cols w:num="2" w:space="708"/></w:sectPr>')
    final = SECTION.replace('<w:sectPr>', '<w:sectPr>' + references).replace('</w:sectPr>', '<w:titlePg/></w:sectPr>')
    return word_docx(
        p(r('Tekst w dwóch kolumnach, w sekcji poziomej.'), properties=landscape)
        + p(r('Tekst w ostatniej sekcji.')), section=final, parts=parts, relationships=relationships,
        overrides=overrides, settings='<w:evenAndOddHeaders/>')


def symbols_and_breaks():
    """Znaki i podziały: ``w:sym`` (Wingdings, jak z LibreOffice), łącznik nierozdzielający i miękki,
    ``w:cr``, ``w:br`` strony, kolumny i wiersza z ``w:clear``, ``w:ptab`` i ``w:tab`` w treści."""
    return word_docx(
        p(r('Symbol '), '<w:r><w:sym w:font="Wingdings" w:char="F04A"/></w:r>', r(' i łą'),
          '<w:r><w:softHyphen/></w:r>', r('cznik, nie'), '<w:r><w:noBreakHyphen/></w:r>', r('rozdzielny.'))
        + p(r('Przed'), '<w:r><w:cr/></w:r>', r('po powrocie karetki.'))
        + p(r('Kolumna'), '<w:r><w:br w:type="column"/></w:r>', r('dalej'),
            '<w:r><w:br w:type="textWrapping" w:clear="all"/></w:r>', r('wiersz.'))
        + p(r('Lewa'), '<w:r><w:ptab w:relativeTo="margin" w:alignment="right" w:leader="none"/></w:r>',
            r('prawa'), '<w:r><w:tab/></w:r>', r('tab.'))
        + p(r('Strona'), '<w:r><w:br w:type="page"/></w:r>', r('następna.')))


def math():
    """Równania OMML: blokowe (m:oMathPara, jak z pandoc) i w tekście (m:oMath)."""
    equation = ('<m:oMath><m:r><m:t>E=m</m:t></m:r><m:sSup><m:e><m:r><m:t>c</m:t></m:r></m:e><m:sup><m:r><m:t>2'
                '</m:t></m:r></m:sup></m:sSup></m:oMath>')
    return word_docx(p(r('Wzór blokowy:')) + p(f'<m:oMathPara>{equation}</m:oMathPara>')
                     + p(r('Wzór w tekście '), equation, r(' i dalszy tekst.')))


def chart_and_smart_art():
    """Wykres (c:chart, jak z LibreOffice) i SmartArt (dgm:relIds) z tekstem w części danych diagramu."""
    data = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<dgm:dataModel xmlns:dgm="http://schemas.openxml'
            'formats.org/drawingml/2006/diagram" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<dgm:ptLst><dgm:pt modelId="{1}"><dgm:t><a:bodyPr/><a:p><a:r><a:t>Etap pierwszy</a:t></a:r></a:p>'
            '</dgm:t></dgm:pt></dgm:ptLst></dgm:dataModel>')
    chart = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<c:chartSpace xmlns:c="http://schemas.openxml'
             'formats.org/drawingml/2006/chart"><c:chart><c:plotArea/></c:chart></c:chartSpace>')
    diagram = 'http://schemas.openxmlformats.org/drawingml/2006/diagram'
    return word_docx(
        p(r('Wykres: '), drawing('<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
                                 '<c:chart r:id="rIdChart"/></a:graphicData>'), r(' koniec.'))
        + p(r('Diagram: '), drawing(f'<a:graphicData uri="{diagram}"><dgm:relIds r:dm="rIdDm" r:lo="rIdDm" '
                                    'r:qs="rIdDm" r:cs="rIdDm"/></a:graphicData>'), r(' koniec.')),
        parts={'word/charts/chart1.xml': chart, 'word/diagrams/data1.xml': data},
        relationships=[('rIdChart', f'{R_NS}/chart', 'charts/chart1.xml'),
                       ('rIdDm', f'{R_NS}/diagramData', 'diagrams/data1.xml')],
        overrides=[('/word/charts/chart1.xml', 'application/vnd.openxmlformats-officedocument.drawingml.chart+xml'),
                   ('/word/diagrams/data1.xml',
                    'application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml')])


def ole_object():
    """Obiekt OLE (arkusz osadzony) z podglądem EMF: w:object z v:shape i o:OLEObject."""
    return word_docx(
        p(r('Arkusz: '), '<w:r><w:object w:dxaOrig="1440" w:dyaOrig="720"><v:shape id="_x0000_i1025" '
          'type="#_x0000_t75" style="width:72pt;height:36pt" o:ole=""><v:imagedata r:id="rIdEmf" o:title=""/>'
          '</v:shape><o:OLEObject Type="Embed" ProgID="Excel.Sheet.12" ShapeID="_x0000_i1025" DrawAspect="Content" '
          'ObjectID="_1700000000" r:id="rIdOle"/></w:object></w:r>', r(' po arkuszu.')),
        parts={'word/media/image1.emf': EMF, 'word/embeddings/Arkusz1.xlsx': b'PK\x05\x06' + b'\x00' * 18},
        relationships=[('rIdEmf', f'{R_NS}/image', 'media/image1.emf'),
                       ('rIdOle', f'{R_NS}/package', 'embeddings/Arkusz1.xlsx')],
        defaults=[('emf', 'image/x-emf'),
                  ('xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')])


def image_docx(name, data, content_type, *, svg=None, extension=None, link=None):
    """Akapit z obrazem (wp:inline) w pliku word/media/<name>; svg – dodatkowy obraz SVG (asvg:svgBlip),
    link – adres obrazu połączonego (r:link, TargetMode External) zamiast osadzonego."""
    relationships = [('rIdImg', f'{R_NS}/image', link or f'media/{name}', *(('External',) if link else ()))]
    parts = {} if link or data is None else {f'word/media/{name}': data}
    blip = ''
    if svg:
        relationships.append(('rIdSvg', f'{R_NS}/image', 'media/obraz.svg'))
        parts['word/media/obraz.svg'] = svg
        blip = ('<a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}"><asvg:svgBlip r:embed="rIdSvg"/></a:ext>')
    defaults = [] if content_type is None else [(extension or name.rsplit('.', 1)[1], content_type)]
    if svg:
        defaults.append(('svg', 'image/svg+xml'))
    return word_docx(p(r('Przed obrazem '), drawing(picture('rIdImg', link=bool(link), extension=blip)),
                       r(' po obrazie.')), parts=parts, relationships=relationships, defaults=defaults)


def styles_cycle():
    """Style z cyklem basedOn (A → B → A, C → C) w stylach akapitu i znaków."""
    styles = STYLES + (
        '<w:style w:type="paragraph" w:styleId="A"><w:name w:val="Styl A"/><w:basedOn w:val="B"/>'
        '<w:pPr><w:jc w:val="center"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="B"><w:name w:val="Styl B"/><w:basedOn w:val="A"/>'
        '<w:rPr><w:i/></w:rPr></w:style>'
        '<w:style w:type="character" w:styleId="C"><w:name w:val="Styl C"/><w:basedOn w:val="C"/>'
        '<w:rPr><w:b/></w:rPr></w:style>')
    return word_docx(p(r('Akapit w stylu z cyklem '), r('i znaki', '<w:rStyle w:val="C"/>'),
                       properties='<w:pStyle w:val="A"/>') + p(r('Akapit w stylu B.'), properties='<w:pStyle w:val="B"/>'),
                     styles=styles)


def localized_style_names():
    """Zlokalizowane nazwy stylów (``w:name`` „Nagłówek 1”, „Nagłówek 2”, „Normalny”, „Tytuł”) przy
    identyfikatorach bez polskich liter (Nagwek1) – tak zapisują niektóre konwertery i starsze pakiety;
    Word 2007+ zapisuje w ``w:name`` angielską nazwę wbudowaną („heading 1”)."""
    styles = (
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normalny"><w:name w:val="Normalny"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Tytu"><w:name w:val="Tytuł"/><w:basedOn w:val="Normalny"/>'
        '<w:rPr><w:sz w:val="56"/></w:rPr></w:style>'
        + ''.join(f'<w:style w:type="paragraph" w:styleId="Nagwek{level}"><w:name w:val="Nagłówek {level}"/>'
                  f'<w:basedOn w:val="Normalny"/><w:pPr><w:outlineLvl w:val="{level - 1}"/></w:pPr><w:rPr><w:b/>'
                  '</w:rPr></w:style>' for level in (1, 2)))
    return word_docx(
        p(r('Tytuł książki'), properties='<w:pStyle w:val="Tytu"/>')
        + p(r('Rozdział pierwszy'), properties='<w:pStyle w:val="Nagwek1"/>') + p(r('Treść pierwszego.'))
        + p(r('Podrozdział'), properties='<w:pStyle w:val="Nagwek2"/>')
        + p(r('Rozdział drugi'), properties='<w:pStyle w:val="Nagwek1"/>') + p(r('Treść drugiego.')),
        styles=styles)


def broken_numbering():
    """Numeracja z odwołaniami donikąd: num → nieistniejący abstractNum, numId bez definicji num,
    poziom ilvl 12 (spoza 0–8) i numId="abc"."""
    numbering = ('<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
                 '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>'
                 '<w:num w:numId="2"><w:abstractNumId w:val="0"/></w:num>')

    def item(text, number, level='0'):
        return p(r(text), properties=f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{number}"/></w:numPr>')
    return word_docx(item('Bez definicji listy', '1') + item('Bez num', '7') + item('Zły poziom', '2', '12')
                     + item('Zły numer', 'abc') + p(r('Zwykły.')), numbering=numbering)


def deep_nesting(depth=6, *, extreme=0):
    """Tabela w tabeli w kontrolce w komórce – depth poziomów; extreme > 0 dokłada akapit z tyloma
    zagnieżdżonymi kontrolkami w:sdt w tekście (granica rekurencji bibliotek)."""
    inner = p(r('Najgłębszy tekst.'))
    for level in range(depth):
        inner = ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="3000"/>'
                 f'</w:tblGrid><w:tr><w:tc><w:sdt><w:sdtPr><w:id w:val="{level + 1}"/></w:sdtPr><w:sdtContent>'
                 + p(r(f'Poziom {level + 1}')) + inner + '</w:sdtContent></w:sdt></w:tc></w:tr></w:tbl>')
    body = p(r('Przed.')) + inner + p(r('Po.'))
    if extreme:
        body += ('<w:p>' + '<w:sdt><w:sdtContent>' * extreme + r('Bardzo głęboko.')
                 + '</w:sdtContent></w:sdt>' * extreme + '</w:p>')
    return word_docx(body)


def rtl_and_east_asian():
    """Tekst od prawej do lewej (w:bidi, w:rtl, arabski i hebrajski), osadzenie kierunku ``w:dir`` i
    ``w:bdo`` (Word 2010+) oraz tekst wschodnioazjatycki z ``w:eastAsianLayout``."""
    return word_docx(
        p(r('نص عربي', '<w:rtl/><w:lang w:bidi="ar-SA"/>'), properties='<w:bidi/><w:jc w:val="right"/>')
        + p(r('Wstawka '), '<w:dir w:val="rtl">' + r('עברית', '<w:rtl/>') + '</w:dir>', r(' i '),
            '<w:bdo w:val="ltr">' + r('LTR') + '</w:bdo>', r(' koniec.'))
        + p(r('日本語のテキスト', '<w:rFonts w:hint="eastAsia"/><w:eastAsianLayout w:id="1" w:combine="1"/>'
              '<w:lang w:eastAsia="ja-JP"/>')))


def encoded_parts(encoding):
    """Części XML w innym kodowaniu: 'utf-16' (document.xml w UTF-16 z BOM) albo 'bom' (UTF-8 z BOM
    w document.xml i styles.xml)."""
    parts = word_parts(p(r('Zażółć gęślą jaźń.')) + p(r('Drugi akapit.')))
    if encoding == 'utf-16':
        text = parts['word/document.xml'].replace('encoding="UTF-8"', 'encoding="UTF-16"')
        parts['word/document.xml'] = text.encode('utf-16')
    else:
        for name in ('word/document.xml', 'word/styles.xml'):
            parts[name] = b'\xef\xbb\xbf' + parts[name].encode('utf-8')
    return package(parts)


def doctype_entities(kind):
    """DOCTYPE w części XML: 'internal' – encja wewnętrzna w document.xml, 'external' – encja SYSTEM do pliku
    serwera (XXE), 'expansion' – zagnieżdżone encje (tzw. billion laughs), 'styles' – DOCTYPE w styles.xml."""
    declarations = {
        'internal': '<!ENTITY nazwa "Fantazmaty">',
        'external': '<!ENTITY nazwa SYSTEM "file:///etc/hostname">',
        'expansion': '<!ENTITY a "aaaaaaaaaa">' + ''.join(
            f'<!ENTITY {chr(98 + level)} "' + f'&{chr(97 + level)};' * 10 + '">' for level in range(8)),
    }
    reference = '&i;' if kind == 'expansion' else '&nazwa;'
    parts = word_parts(p(r('Tekst ')) + p(r('Koniec.')))
    name = 'word/styles.xml' if kind == 'styles' else 'word/document.xml'
    root = 'w:styles' if kind == 'styles' else 'w:document'
    declaration = declarations.get(kind, declarations['internal'])
    parts[name] = parts[name].replace(XML_HEADER, f'{XML_HEADER}<!DOCTYPE {root} [{declaration}]>')
    if kind != 'styles':
        parts[name] = parts[name].replace('<w:t xml:space="preserve">Tekst </w:t>',
                                          f'<w:t xml:space="preserve">Tekst {reference}</w:t>')
    return package(parts)


def package_variant(kind):
    """Nietypowy pakiet ZIP/OPC z tą samą treścią: 'absolute_target' (Target „/word/document.xml”),
    'renamed_main' (część główna word/document2.xml), 'case_mismatch' (Target „Styles.xml” przy
    word/styles.xml), 'backslash_target' (Target z ukośnikiem wstecznym), 'backslash_entry' (wpis ZIP
    „word\\media\\x.png”), 'duplicate' (powielony wpis), 'zip64', 'descriptor' (deskryptory danych),
    'stored' (bez kompresji), 'jpg_upper' (obraz .JPG przy Default „jpg”), 'no_default' (obraz bez
    typu zawartości), 'image_jpg' (typ „image/jpg”), 'missing_part' (relacja do nagłówka, którego nie ma
    w pakiecie)."""
    body = p(r('Treść pakietu.')) + p(r('Drugi akapit.'))
    if kind == 'absolute_target':
        return word_docx(body, package_target='/word/document.xml')
    if kind == 'renamed_main':
        return word_docx(body, document_name='word/document2.xml')
    if kind in ('jpg_upper', 'no_default', 'image_jpg'):
        name = 'obraz.JPG' if kind == 'jpg_upper' else 'obraz.jpg'
        content_type = {'jpg_upper': 'image/jpeg', 'no_default': None, 'image_jpg': 'image/jpg'}[kind]
        return image_docx(name, PNG, content_type, extension='jpg')
    parts = word_parts(body)
    if kind == 'case_mismatch':
        parts['word/_rels/document.xml.rels'] = parts['word/_rels/document.xml.rels'].replace(
            'Target="styles.xml"', 'Target="Styles.xml"')
        return package(parts)
    if kind == 'backslash_target':
        parts['word/_rels/document.xml.rels'] = parts['word/_rels/document.xml.rels'].replace(
            'Target="theme/theme1.xml"', 'Target="theme\\theme1.xml"')
        return package(parts)
    if kind == 'missing_part':
        return word_docx(body, relationships=[('rIdLost', f'{R_NS}/header', 'header9.xml')])
    if kind == 'backslash_entry':
        parts['word\\media\\x.png'] = PNG
        return package(parts)
    return package(parts, duplicate='word/document.xml' if kind == 'duplicate' else None,
                   zip64=kind == 'zip64', descriptor=kind == 'descriptor', stored=kind == 'stored')


def degenerate(kind):
    """Dokumenty skrajnie ubogie: 'empty' (w:body z samym w:sectPr), 'no_section' (bez w:sectPr), 'only_image'
    (jeden akapit z obrazem), 'only_table' (sama tabela, bez akapitów poza nią), 'no_runs' (akapity bez w:r,
    także z samym w:pPr i zakładką)."""
    if kind == 'empty':
        return word_docx('')
    if kind == 'no_section':
        return word_docx(p(r('Akapit bez sekcji.')), section='')
    if kind == 'only_image':
        return word_docx(p(drawing(picture('rIdImg'))), parts={'word/media/image1.png': PNG},
                         relationships=[('rIdImg', f'{R_NS}/image', 'media/image1.png')], defaults=[('png', 'image/png')])
    if kind == 'only_table':
        return word_docx('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="4000"/>'
                         '</w:tblGrid><w:tr><w:tc>' + p(r('Jedyna komórka')) + '</w:tc></w:tr></w:tbl>')
    if kind == 'no_runs':
        return word_docx('<w:p/><w:p><w:pPr><w:jc w:val="center"/></w:pPr></w:p>'
                         '<w:p><w:bookmarkStart w:id="0" w:name="pusta"/><w:bookmarkEnd w:id="0"/></w:p><w:p/>')
    raise ValueError(kind)


def document_kind(kind):
    """Dokument z typem zawartości części głównej: docm (z makrami, z częścią vbaProject), dotx, dotm."""
    parts, relationships = {}, []
    if kind in ('docm', 'dotm'):
        parts['word/vbaProject.bin'] = bytes.fromhex('d0cf11e0a1b11ae1') + b'\x00' * 504
        relationships.append(('rIdVba', f'{OFFICE}/2006/relationships/vbaProject', 'vbaProject.bin'))
    return word_docx(p(r('Dokument typu ' + kind + '.')), kind=kind, parts=parts, relationships=relationships,
                     defaults=[('bin', 'application/vnd.ms-office.vbaProject')] if parts else [])


# Edytory i elementy problematyczne: nazwa → funkcja budująca plik (strumień na początku).
EDITOR_SCENARIOS = {
    'word365': word365_docx,
    'word2007': word2007_docx,
    'wps': wps_docx,
    'onlyoffice': onlyoffice_docx,
    'pages': pages_docx,
    'google_docs_extras': google_docs_extras_docx,
    'minimal': minimal_docx,
}
FEATURE_SCENARIOS = {
    'complex_fields': complex_fields,
    'simple_fields': simple_fields,
    'content_controls': content_controls,
    'alt_chunk': alt_chunk,
    'tracked_changes': tracked_changes,
    'review': review_docx,
    'libreoffice_review': libreoffice_review_docx,
    'unusual_notes': unusual_notes,
    'frames_and_drop_cap': frames_and_drop_cap,
    'sections_and_columns': sections_and_columns,
    'symbols_and_breaks': symbols_and_breaks,
    'math': math,
    'chart_and_smart_art': chart_and_smart_art,
    'ole_object': ole_object,
    'image_emf': lambda: image_docx('obraz.emf', EMF, 'image/x-emf'),
    'image_wmf': lambda: image_docx('obraz.wmf', WMF, 'image/x-wmf'),
    'image_svg': lambda: image_docx('obraz.png', PNG, 'image/png',
                                    svg='<svg xmlns="http://www.w3.org/2000/svg" width="4" height="2"/>'),
    'image_heic': lambda: image_docx('obraz.heic', HEIC, 'image/heic'),
    'image_missing_part': lambda: image_docx('obraz.png', None, 'image/png'),
    'image_broken': lambda: image_docx('obraz.png', b'\x89PNG to nie jest obraz', 'image/png'),
    # Obraz połączony z plikiem zewnętrznym: proces roboczy usuwa link (i obraz), plik nie jest odrzucany.
    'image_linked': lambda: image_docx('obraz.png', PNG, 'image/png', link='file:///C:/Obrazy/zdjecie.jpg'),
    'styles_cycle': styles_cycle,
    'localized_style_names': localized_style_names,
    'broken_numbering': broken_numbering,
    'deep_nesting': deep_nesting,
    'rtl_and_east_asian': rtl_and_east_asian,
    'utf16_part': lambda: encoded_parts('utf-16'),
    'utf8_bom': lambda: encoded_parts('bom'),
    'empty': lambda: degenerate('empty'),
    'no_section': lambda: degenerate('no_section'),
    'only_image': lambda: degenerate('only_image'),
    'only_table': lambda: degenerate('only_table'),
    'no_runs': lambda: degenerate('no_runs'),
    **{f'package_{kind}': (lambda kind=kind: package_variant(kind)) for kind in (
        'absolute_target', 'renamed_main', 'zip64', 'descriptor', 'stored', 'jpg_upper', 'no_default',
        'image_jpg')},
}
# Pliki odrzucane przed konwersją (zawartość niebezpieczna albo nie-DOCX): nazwa → funkcja budująca.
REFUSED_SCENARIOS = {
    'strict': strict_docx,
    'doctype_internal': lambda: doctype_entities('internal'),
    'doctype_external': lambda: doctype_entities('external'),
    'doctype_expansion': lambda: doctype_entities('expansion'),
    'doctype_styles': lambda: doctype_entities('styles'),
    'package_backslash_target': lambda: package_variant('backslash_target'),
    'package_backslash_entry': lambda: package_variant('backslash_entry'),
    'package_duplicate': lambda: package_variant('duplicate'),
    'package_case_mismatch': lambda: package_variant('case_mismatch'),
    'package_missing_part': lambda: package_variant('missing_part'),
    **{kind: (lambda kind=kind: document_kind(kind)) for kind in ('docm', 'dotx', 'dotm')},
    **{f'foreign_{kind}': (lambda kind=kind: BytesIO(foreign_file(kind)))
       for kind in ('doc', 'encrypted', 'rtf', 'odt', 'word2003', 'html')},
}
