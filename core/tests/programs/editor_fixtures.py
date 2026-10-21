"""Syntetyczne DOCX „jak z LibreOffice i Google Docs” – stały zestaw testów tolerancji konwertera.

Pliki są budowane od zera (ZipFile + ręczny XML, bez python-docx), żeby wiernie odtworzyć zapis tych
edytorów. Wzorcem są dwie próbki z LibreOffice 24.2 (ten sam dokument zapisany jako „Word 2007–365”,
czyli OOXML Transitional, oraz „Office Open XML Text”, czyli nazwy wartości ze Strict); cechy Google Docs
pochodzą ze znajomości jego eksportu (nie z próbki w repo) i są tak oznaczone w docstringu.

Cechy LibreOffice odczytane z próbek (obie odmiany, o ile nie zaznaczono inaczej):

* pakiet: ``_rels/.rels`` z core/app/custom, ``docProps/custom.xml`` (pusty), ``word/theme/theme1.xml``,
  ``[Content_Types].xml`` z Override także dla ``/_rels/.rels`` i ``/word/_rels/document.xml.rels``;
  w odmianie Strict typ relacji core-properties ma małe litery ``officedocument`` (python-docx wtedy nie
  widzi właściwości dokumentu);
* ``docProps/app.xml``: ``<Application>LibreOffice/24.2…</Application>``, ``<AppVersion>15.0000``;
* ``settings.xml``: tylko ``w:zoom``, ``w:defaultTabStop 720``, ``w:autoHyphenation`` i ``w:compat`` z
  ``compatibilityMode`` 12 (Transitional) albo 15 (Strict); bez ``w:rsids`` i ``w:proofState``;
* ``styles.xml``: ``w:docDefaults`` z ``w:rFonts`` (Cambria), ``w:sz 22``, ``w:lang en-US`` i
  ``w:pPrDefault`` z ``w:suppressAutoHyphens``; styl ``Normal`` (nie ma ``w:default``) z pełnym
  ``w:pPr`` (``w:widowControl``, ``w:kinsoku``, ``w:overflowPunct``, ``w:autoSpaceDE``, ``w:bidi 0``,
  ``w:spacing lineRule=auto line=276 before=0 after=200``, ``w:jc``) i ``w:rPr`` (``w:kern 0``,
  ``w:color auto``); identyfikatory stylów jak w Wordzie (``Heading1``, ``ListBullet``, ``ListNumber``,
  ``Title``…) plus własne LibreOffice (``Heading``, ``List``, ``Index``, ``HeaderandFooter``);
  nagłówki mają ``w:numPr`` z ``w:numId 0``, ``w:keepNext w:val="true"`` i ``w:keepLines``;
* ``w:jc``: ``left``/``right`` (Transitional) albo ``start``/``end`` (Strict), ``center`` i ``both``
  w obu; ``w:ind``: ``w:left``/``w:right`` albo ``w:start``/``w:end``; ``w:lvlJc`` tak samo;
* każdy akapit ma ``w:pStyle`` (także ``Normal``) i pusty ``w:rPr`` w ``w:pPr``; każdy przebieg ma
  ``w:rPr`` (choćby pusty); wartości logiczne jako ``w:val="true"``/``"false"``;
* akapit, który w źródle nie miał własnego formatowania, dostaje je jawnie z Normal (``w:widowControl``,
  ``w:bidi w:val="0"``, ``w:spacing``, ``w:jc``) – ostatni akapit próbki;
* tabulatory: ``<w:tab w:val="clear" w:pos="720"/>`` przed własnym tabulatorem z ``w:leader="dot"``;
* numeracja: ``w:abstractNum`` 1 to „bez numeracji” (``w:numFmt none``, ``w:suff nothing``), punktory
  z czcionką Symbol (``w:lvlText`` z U+F0B7 w PUA), ``w:tab w:val="num"``; akapity list mają
  ``w:pStyle ListBullet``/``ListNumber`` i jawne ``w:numPr`` (``w:numId`` 2 i 5);
* ``fontTable.xml``: w Strict ``w:charset`` ma dodatkowo ``w:characterSet="windows-1252"``;
* ``w:sectPr`` z ``w:type nextPage``, Letter (12240×15840), ``w:pgMar`` z ``w:header="0"``/``w:footer="0"``,
  ``w:pgNumType``, ``w:formProt``, ``w:textDirection lrTb`` i ``w:docGrid … w:charSpace="4096"``;
* próbki nie mają ``w:proofErr``, zakładek ``_GoBack`` ani ``w:rsid``.

Cechy spoza próbek (według znajomości LibreOffice): przypisy – separatory mają ``w:id`` 0 i 1,
pierwszy przypis ``w:id`` 2, odsyłacz ma styl ``FootnoteAnchor``, treść styl ``Footnote``
(nazwa „footnote text”), w ``w:sectPr`` jest ``w:footnotePr`` z ``w:numFmt``, a w ``settings.xml``
``w:footnotePr`` wymienia oba separatory; nagłówek i stopka to ``header1.xml``/``footer1.xml`` ze
stylami ``Header``/``Footer``; pole tekstowe to ``mc:AlternateContent`` (wps + zapasowy VML ``v:rect``);
miękki enter to ``<w:br/>`` w przebiegu.
"""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
DOCUMENT_NAMESPACES = (
    'xmlns:o="urn:schemas-microsoft-com:office:office" '
    f'xmlns:r="{R_NS}" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="{W_NS}" '
    'xmlns:w10="urn:schemas-microsoft-com:office:word" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" mc:Ignorable="w14 wp14 w15"'
)

# Treść testowa wspólna dla wszystkich edytorów: proste cudzysłowy i podwójna spacja do poprawienia
# przez Odkurzacz, wyrównania, formatowanie znaków, tabulator, listy, przypis, miękki enter.
HEADING = 'Nagłówek 1'
ALIGNED = {
    'left': 'Akapit "do lewej" z  podwójną spacją.',
    'right': 'Akapit do prawej.',
    'center': 'Akapit wyśrodkowany.',
    'both': 'Akapit wyjustowany.',
}
BOLD, ITALIC, UNDERLINE = 'Pogrubienie', ' kursywa', ' podkreślenie'
TAB_BEFORE, TAB_AFTER = 'Tab', 'stop'
BULLET, NUMBER = 'Lista', 'Numer'
NOTED, NOTE_TEXT = 'Przypis', 'Treść "przypisu".'
SOFT_FIRST, SOFT_SECOND = 'Pierwsza linia', 'druga linia'
LAST = 'Koniec.'
HEADER_TEXT, FOOTER_TEXT = 'Nagłówek strony', 'Stopka strony'
TEXT_BOX = 'Pole tekstowe'
LINK_TEXT = 'odnośnik'

# Jak Odkurzacz poprawia treść testową (cudzysłowy polskie, jedna spacja).
CLEANED = {
    ALIGNED['left']: 'Akapit „do lewej” z podwójną spacją.',
    NOTE_TEXT: 'Treść „przypisu”.',
}


def expected_texts(*, lists=True, footnotes=False, soft_break_split=False, cleaned=False):
    """Teksty akapitów treści głównej w kolejności, jak zwraca je python-docx.

    soft_break_split – po zamianie miękkich enterów na akapity; cleaned – po Odkurzaczu.
    """
    texts = [HEADING, *ALIGNED.values(), BOLD + ITALIC + UNDERLINE, TAB_BEFORE + '\t' + TAB_AFTER]
    if lists:
        texts += [BULLET, NUMBER]
    texts.append(NOTED)
    if soft_break_split:
        texts += [SOFT_FIRST, SOFT_SECOND]
    else:
        texts.append(SOFT_FIRST + '\n' + SOFT_SECOND)
    texts.append(LAST)
    if cleaned:
        texts = [CLEANED.get(text, text) for text in texts]
    return texts


def _escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _t(text):
    space = ' xml:space="preserve"' if text != text.strip() or '  ' in text else ''
    return f'<w:t{space}>{_escape(text)}</w:t>'


def _run(text, properties=''):
    return f'<w:r><w:rPr>{properties}</w:rPr>{_t(text)}</w:r>'


def _rels(relationships):
    """Część relacji: lista (Id, Type, Target[, TargetMode])."""
    items = ''.join(
        f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"'
        + (f' TargetMode="{mode}"' if mode else '') + '/>'
        for rid, kind, target, *rest in relationships for mode in (rest[0] if rest else None,)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="{REL_NS}">{items}</Relationships>'


def _content_types(overrides, *, package_rels=True):
    """[Content_Types].xml: LibreOffice dodaje Override dla części relacji, Google Docs nie."""
    defaults = ('<Default Extension="xml" ContentType="application/xml"/>'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="png" ContentType="image/png"/>'
                '<Default Extension="jpeg" ContentType="image/jpeg"/>')
    if package_rels:
        overrides = [('/_rels/.rels', 'application/vnd.openxmlformats-package.relationships+xml'), *overrides]
    items = ''.join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in overrides)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'content-types">{defaults}{items}</Types>')


WORD_PART = 'application/vnd.openxmlformats-officedocument.wordprocessingml.{}+xml'


def _package(parts):
    """ZIP DOCX w pamięci (strumień na początku) z części {nazwa: tekst XML}."""
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data.encode('utf-8') if isinstance(data, str) else data)
    output.seek(0)
    return output


# --- LibreOffice -----------------------------------------------------------------------------------------

LO_LEVELS = {
    'transitional': {'left': 'left', 'right': 'right', 'ind_left': 'left', 'ind_right': 'right', 'compat': '12',
                     'charset': '', 'core_rel': 'officeDocument'},
    # Odmiana „Office Open XML Text”: nazwy wartości ze Strict, compat 15, kodowanie w tabeli czcionek
    # i (błąd LibreOffice) typ relacji właściwości dokumentu z małymi literami.
    'strict': {'left': 'start', 'right': 'end', 'ind_left': 'start', 'ind_right': 'end', 'compat': '15',
               'charset': ' w:characterSet="windows-1252"', 'core_rel': 'officedocument'},
}


def _lo_heading_style(number):
    return (f'<w:style w:type="paragraph" w:styleId="Heading{number}"><w:name w:val="Heading {number}"/>'
            f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:link w:val="Heading{number}Char"/><w:qFormat/>'
            '<w:pPr><w:keepNext w:val="true"/><w:keepLines/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="0"/>'
            f'</w:numPr><w:spacing w:before="{480 if number == 1 else 200}" w:after="0"/>'
            f'<w:outlineLvl w:val="{number - 1}"/></w:pPr>'
            '<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="ＭＳ ゴシック" w:cs="DejaVu Sans"/>'
            '<w:b/><w:bCs/><w:color w:themeColor="accent1" w:themeShade="bf" w:val="365F91"/>'
            f'<w:sz w:val="{28 if number == 1 else 26}"/><w:szCs w:val="{28 if number == 1 else 26}"/></w:rPr></w:style>'
            f'<w:style w:type="character" w:styleId="Heading{number}Char"><w:name w:val="Heading {number} Char"/>'
            '<w:basedOn w:val="DefaultParagraphFont"/><w:rPr><w:b/></w:rPr></w:style>')


def _lo_styles(names, *, footnotes, header_footer):
    normal_ppr = ('<w:widowControl/><w:kinsoku w:val="true"/><w:overflowPunct w:val="true"/>'
                  '<w:autoSpaceDE w:val="true"/><w:bidi w:val="0"/>'
                  '<w:spacing w:lineRule="auto" w:line="276" w:before="0" w:after="200"/>'
                  f'<w:jc w:val="{names["left"]}"/>')
    normal_rpr = ('<w:rFonts w:ascii="Cambria" w:hAnsi="Cambria" w:eastAsia="ＭＳ 明朝"/><w:color w:val="auto"/>'
                  '<w:kern w:val="0"/><w:sz w:val="22"/><w:szCs w:val="22"/>'
                  '<w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/>')
    styles = [
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Cambria" w:hAnsi="Cambria" w:eastAsia="ＭＳ 明朝" w:cs="DejaVu Sans"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/>'
        '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:suppressAutoHyphens w:val="true"/></w:pPr></w:pPrDefault>'
        '</w:docDefaults>',
        f'<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/><w:pPr>{normal_ppr}'
        f'</w:pPr><w:rPr>{normal_rpr}</w:rPr></w:style>',
        _lo_heading_style(1),
        _lo_heading_style(2),
        '<w:style w:type="character" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/>'
        '<w:qFormat/><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="Heading"><w:name w:val="Heading"/><w:basedOn w:val="Normal"/>'
        '<w:next w:val="BodyText"/><w:qFormat/><w:pPr><w:keepNext w:val="true"/>'
        '<w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr>'
        '<w:rFonts w:ascii="Liberation Sans" w:hAnsi="Liberation Sans" w:eastAsia="Noto Sans CJK SC" '
        'w:cs="Noto Sans Devanagari"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>',
        '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:spacing w:before="0" w:after="120"/></w:pPr><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="List"><w:name w:val="List"/><w:basedOn w:val="Normal"/>'
        f'<w:pPr><w:spacing w:before="0" w:after="200"/><w:ind w:hanging="360" w:{names["ind_left"]}="360"/>'
        '<w:contextualSpacing/></w:pPr><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="Index"><w:name w:val="Index"/><w:basedOn w:val="Normal"/>'
        '<w:qFormat/><w:pPr><w:suppressLineNumbers/></w:pPr><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
        '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" '
        'w:color="4F81BD"/></w:pBdr><w:spacing w:lineRule="auto" w:line="240" w:before="0" w:after="300"/>'
        '<w:contextualSpacing/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
        '<w:color w:themeColor="dark2" w:themeShade="bf" w:val="17365D"/><w:spacing w:val="5"/><w:kern w:val="2"/>'
        '<w:sz w:val="52"/><w:szCs w:val="52"/></w:rPr></w:style>',
        '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
        '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="0" w:after="200"/>'
        f'<w:ind w:{names["ind_left"]}="720"/><w:contextualSpacing/></w:pPr><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr><w:spacing w:before="0" w:after="200"/>'
        '<w:contextualSpacing/></w:pPr><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="ListNumber"><w:name w:val="List Number"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr><w:spacing w:before="0" w:after="200"/>'
        '<w:contextualSpacing/></w:pPr><w:rPr/></w:style>',
        '<w:style w:type="paragraph" w:styleId="IntenseQuote"><w:name w:val="Intense Quote"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:pBdr><w:bottom w:val="single" '
        'w:sz="4" w:space="4" w:color="4F81BD"/></w:pBdr><w:spacing w:before="200" w:after="280"/>'
        f'<w:ind w:{names["ind_left"]}="936" w:{names["ind_right"]}="936"/></w:pPr><w:rPr><w:b/><w:bCs/><w:i/>'
        '<w:iCs/><w:color w:themeColor="accent1" w:val="4F81BD"/></w:rPr></w:style>',
        '<w:style w:type="numbering" w:styleId="NoList"><w:name w:val="No List"/><w:qFormat/><w:rPr/></w:style>',
    ]
    if footnotes:
        styles += [
            '<w:style w:type="paragraph" w:styleId="Footnote"><w:name w:val="footnote text"/>'
            '<w:basedOn w:val="Normal"/><w:pPr><w:suppressLineNumbers/>'
            f'<w:ind w:{names["ind_left"]}="339" w:hanging="339"/></w:pPr><w:rPr><w:sz w:val="20"/>'
            '<w:szCs w:val="20"/></w:rPr></w:style>',
            '<w:style w:type="character" w:styleId="FootnoteAnchor"><w:name w:val="Footnote Anchor"/>'
            '<w:qFormat/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr></w:style>',
            '<w:style w:type="character" w:styleId="FootnoteCharacters"><w:name w:val="Footnote Characters"/>'
            '<w:qFormat/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr></w:style>',
        ]
    if header_footer:
        styles += [
            '<w:style w:type="paragraph" w:styleId="HeaderandFooter"><w:name w:val="Header and Footer"/>'
            '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:tabs><w:tab w:val="center" w:pos="4680" '
            'w:leader="none"/><w:tab w:val="right" w:pos="9360" w:leader="none"/></w:tabs></w:pPr><w:rPr/></w:style>',
            '<w:style w:type="paragraph" w:styleId="Header"><w:name w:val="Header"/><w:basedOn w:val="Normal"/>'
            '<w:link w:val="HeaderChar"/><w:pPr><w:tabs><w:tab w:val="clear" w:pos="720"/><w:tab w:val="center" '
            'w:pos="4680" w:leader="none"/><w:tab w:val="right" w:pos="9360" w:leader="none"/></w:tabs>'
            '<w:spacing w:before="0" w:after="0"/></w:pPr><w:rPr/></w:style>',
            '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="Footer"/><w:basedOn w:val="Normal"/>'
            '<w:link w:val="FooterChar"/><w:pPr><w:tabs><w:tab w:val="clear" w:pos="720"/><w:tab w:val="center" '
            'w:pos="4680" w:leader="none"/><w:tab w:val="right" w:pos="9360" w:leader="none"/></w:tabs>'
            '<w:spacing w:before="0" w:after="0"/></w:pPr><w:rPr/></w:style>',
        ]
    return (f'{XML_HEADER}<w:styles xmlns:w="{W_NS}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="w14">'
            + ''.join(styles) + '</w:styles>')


def _lo_numbering(names):
    """Lista 1 „bez numeracji” (dla nagłówków), 2 punktowana (Symbol), 5 numerowana – jak w próbce."""
    def level(ilvl, fmt, text, position, extra=''):
        suffix = '<w:suff w:val="nothing"/>' if fmt == 'none' else ''
        return (f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>{suffix}'
                f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="{names["left"]}"/><w:pPr><w:tabs>'
                f'<w:tab w:val="num" w:pos="{position}"/></w:tabs><w:ind w:{names["ind_left"]}="{position}" '
                f'w:hanging="{360 if position else 0}"/></w:pPr>{extra}</w:lvl>')
    symbol = '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:cs="Symbol" w:hint="default"/></w:rPr>'
    abstracts = {
        1: ''.join(level(i, 'none', f'%{i + 1}', 0) for i in range(9)),
        2: level(0, 'bullet', '', 360, symbol) + ''.join(
            level(i, 'decimal', f'%{i + 1}.', 360 * (i + 2)) for i in range(1, 9)),
        5: ''.join(level(i, 'decimal', f'%{i + 1}.', 360 * (i + 1)) for i in range(9)),
    }
    body = ''.join(f'<w:abstractNum w:abstractNumId="{number}">{levels}</w:abstractNum>'
                   for number, levels in abstracts.items())
    body += ''.join(f'<w:num w:numId="{number}"><w:abstractNumId w:val="{number}"/></w:num>' for number in abstracts)
    return (f'{XML_HEADER}<w:numbering xmlns:w="{W_NS}" xmlns:o="urn:schemas-microsoft-com:office:office" '
            f'xmlns:r="{R_NS}" xmlns:v="urn:schemas-microsoft-com:vml" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14">'
            + body + '</w:numbering>')


def _lo_font_table(names):
    fonts = (('Times New Roman', 'roman', ''), ('Symbol', 'roman', ''), ('Arial', 'swiss', ''),
             ('Liberation Serif', 'roman', 'Times New Roman'), ('Cambria', 'roman', ''), ('Calibri', 'roman', ''))
    items = ''.join(
        f'<w:font w:name="{name}">' + (f'<w:altName w:val="{alt}"/>' if alt else '')
        + f'<w:charset w:val="{"02" if name == "Symbol" else "00"}"{names["charset"]}/>'
        f'<w:family w:val="{family}"/><w:pitch w:val="variable"/></w:font>'
        for name, family, alt in fonts)
    return f'{XML_HEADER}<w:fonts xmlns:w="{W_NS}" xmlns:r="{R_NS}">{items}</w:fonts>'


def _lo_text_box():
    """Pole tekstowe jak z LibreOffice: wersja wps i zapasowa VML (v:rect z v:textbox)."""
    box = f'<w:txbxContent><w:p><w:pPr><w:pStyle w:val="Normal"/><w:rPr/></w:pPr>{_run(TEXT_BOX)}</w:p></w:txbxContent>'
    return (
        '<w:r><w:rPr/><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>'
        '<wp:anchor behindDoc="0" distT="0" distB="0" distL="0" distR="0" simplePos="0" locked="0" layoutInCell="0" '
        'allowOverlap="1" relativeHeight="2"><wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column">'
        '<wp:posOffset>0</wp:posOffset></wp:positionH><wp:positionV relativeFrom="paragraph">'
        '<wp:posOffset>0</wp:posOffset></wp:positionV><wp:extent cx="1800000" cy="360000"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapSquare wrapText="largest"/>'
        '<wp:docPr id="1" name="Ramka1"/><wp:cNvGraphicFramePr/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData '
        'uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:wsp><wps:cNvSpPr/>'
        '<wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1800000" cy="360000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr>'
        f'<wps:txbx>{box}</wps:txbx><wps:bodyPr/></wps:wsp></a:graphicData></a:graphic></wp:anchor>'
        '</w:drawing></mc:Choice><mc:Fallback><w:pict><v:rect id="shape_0" style="position:absolute;'
        f'width:141.7pt;height:28.3pt"><v:textbox>{box}</v:textbox></v:rect></w:pict></mc:Fallback>'
        '</mc:AlternateContent></w:r>'
    )


def _lo_paragraph(style, properties, runs):
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/>{properties}<w:rPr/></w:pPr>{runs}</w:p>'


def libreoffice_docx(variant='transitional', *, footnotes=False, lists=True, header_footer=False, text_box=False):
    """DOCX zapisany „jak przez LibreOffice 24.2” (cechy w docstringu modułu), strumień na początku.

    variant – 'transitional' („Word 2007–365”: w:jc left/right) albo 'strict' („Office Open XML Text”:
    w:jc start/end, w:ind start/end, compat 15). footnotes – akapit „Przypis” z odsyłaczem do przypisu
    (w:id 2; separatory 0 i 1); lists – akapity listy punktowanej i numerowanej; header_footer –
    nagłówek i stopka strony; text_box – pole tekstowe (mc:AlternateContent) w ostatnim akapicie.
    """
    names = LO_LEVELS[variant]
    paragraphs = [
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/><w:keepNext w:val="true"/><w:keepLines/><w:numPr>'
        '<w:ilvl w:val="0"/><w:numId w:val="0"/></w:numPr><w:spacing w:before="480" w:after="0"/><w:rPr/></w:pPr>'
        + _run(HEADING) + '</w:p>',
    ]
    for key, text in ALIGNED.items():
        paragraphs.append(_lo_paragraph('Normal', f'<w:jc w:val="{names.get(key, key)}"/>', _run(text)))
    paragraphs.append(_lo_paragraph('Normal', '', _run(BOLD, '<w:b/>') + _run(ITALIC, '<w:i/>')
                                    + _run(UNDERLINE, '<w:u w:val="single"/>')))
    paragraphs.append(_lo_paragraph(
        'Normal', '<w:tabs><w:tab w:val="clear" w:pos="720"/><w:tab w:val="right" w:pos="2835" w:leader="dot"/></w:tabs>',
        f'<w:r><w:rPr/>{_t(TAB_BEFORE)}<w:tab/>{_t(TAB_AFTER)}</w:r>'))
    if lists:
        paragraphs.append(_lo_paragraph(
            'ListBullet', '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>', _run(BULLET)))
        paragraphs.append(_lo_paragraph(
            'ListNumber', '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr>', _run(NUMBER)))
    reference = ('<w:r><w:rPr><w:rStyle w:val="FootnoteAnchor"/></w:rPr><w:footnoteReference w:id="2"/></w:r>'
                 if footnotes else '')
    paragraphs.append(_lo_paragraph('Normal', '', _run(NOTED) + reference))
    paragraphs.append(_lo_paragraph('Normal', '', f'<w:r><w:rPr/>{_t(SOFT_FIRST)}<w:br/>{_t(SOFT_SECOND)}</w:r>'))
    # Ostatni akapit: LibreOffice zapisuje mu jawnie formatowanie stylu Normal (tak w próbce).
    paragraphs.append(_lo_paragraph(
        'Normal', '<w:widowControl/><w:bidi w:val="0"/><w:spacing w:lineRule="auto" w:line="276" w:before="0" '
        f'w:after="200"/><w:jc w:val="{names["left"]}"/>', _run(LAST) + (_lo_text_box() if text_box else '')))
    relationships = [
        ('rId1', f'{R_NS}/styles', 'styles.xml'),
        ('rId2', f'{R_NS}/numbering', 'numbering.xml'),
        ('rId3', f'{R_NS}/fontTable', 'fontTable.xml'),
        ('rId4', f'{R_NS}/settings', 'settings.xml'),
        ('rId5', f'{R_NS}/theme', 'theme/theme1.xml'),
    ]
    overrides = [
        ('/docProps/core.xml', 'application/vnd.openxmlformats-package.core-properties+xml'),
        ('/docProps/app.xml', 'application/vnd.openxmlformats-officedocument.extended-properties+xml'),
        ('/docProps/custom.xml', 'application/vnd.openxmlformats-officedocument.custom-properties+xml'),
        ('/word/_rels/document.xml.rels', 'application/vnd.openxmlformats-package.relationships+xml'),
        ('/word/document.xml', WORD_PART.format('document.main')),
        ('/word/styles.xml', WORD_PART.format('styles')),
        ('/word/numbering.xml', WORD_PART.format('numbering')),
        ('/word/fontTable.xml', WORD_PART.format('fontTable')),
        ('/word/settings.xml', WORD_PART.format('settings')),
        ('/word/theme/theme1.xml', 'application/vnd.openxmlformats-officedocument.theme+xml'),
    ]
    parts = {}
    section_extra = ''
    settings_notes = ''
    if footnotes:
        relationships.append(('rId6', f'{R_NS}/footnotes', 'footnotes.xml'))
        overrides.append(('/word/footnotes.xml', WORD_PART.format('footnotes')))
        parts['word/footnotes.xml'] = (
            f'{XML_HEADER}<w:footnotes {DOCUMENT_NAMESPACES}>'
            '<w:footnote w:id="0" w:type="separator"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:id="1" w:type="continuationSeparator"><w:p><w:r><w:continuationSeparator/></w:r></w:p>'
            '</w:footnote><w:footnote w:id="2"><w:p><w:pPr><w:pStyle w:val="Footnote"/><w:rPr/></w:pPr>'
            '<w:r><w:rPr><w:rStyle w:val="FootnoteCharacters"/></w:rPr><w:footnoteRef/></w:r>'
            + _run(' ' + NOTE_TEXT) + '</w:p></w:footnote></w:footnotes>')
        section_extra += '<w:footnotePr><w:numFmt w:val="decimal"/></w:footnotePr>'
        settings_notes = '<w:footnotePr><w:footnote w:id="0"/><w:footnote w:id="1"/></w:footnotePr>'
    if header_footer:
        for number, (kind, tag, text) in enumerate((('header', 'hdr', HEADER_TEXT), ('footer', 'ftr', FOOTER_TEXT)), 7):
            relationships.append((f'rId{number}', f'{R_NS}/{kind}', f'{kind}1.xml'))
            overrides.append((f'/word/{kind}1.xml', WORD_PART.format(kind)))
            parts[f'word/{kind}1.xml'] = (
                f'{XML_HEADER}<w:{tag} {DOCUMENT_NAMESPACES}>'
                + _lo_paragraph(kind.capitalize(), '', _run(text)) + f'</w:{tag}>')
            section_extra = f'<w:{kind}Reference w:type="default" r:id="rId{number}"/>' + section_extra
    margins = '720' if header_footer else '0'
    section = (
        f'<w:sectPr>{section_extra}<w:type w:val="nextPage"/><w:pgSz w:w="12240" w:h="15840"/>'
        f'<w:pgMar w:left="1800" w:right="1800" w:gutter="0" w:header="{margins}" w:top="1440" w:footer="{margins}" '
        'w:bottom="1440"/><w:pgNumType w:fmt="decimal"/><w:formProt w:val="false"/>'
        '<w:textDirection w:val="lrTb"/><w:docGrid w:type="default" w:linePitch="360" w:charSpace="4096"/></w:sectPr>'
    )
    parts.update({
        '_rels/.rels': _rels([
            ('rId1', f'http://schemas.openxmlformats.org/{names["core_rel"]}/2006/relationships/metadata/core-properties',
             'docProps/core.xml'),
            ('rId2', f'{R_NS}/extended-properties', 'docProps/app.xml'),
            ('rId3', f'{R_NS}/custom-properties', 'docProps/custom.xml'),
            ('rId4', f'{R_NS}/officeDocument', 'word/document.xml'),
        ]),
        'docProps/core.xml': (
            f'{XML_HEADER}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
            'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T10:00:00Z</dcterms:created>'
            '<dc:creator>Autorka</dc:creator><dc:description></dc:description><dc:language>pl-PL</dc:language>'
            '<cp:lastModifiedBy></cp:lastModifiedBy><dcterms:modified xsi:type="dcterms:W3CDTF">2026-01-01T10:00:00Z'
            '</dcterms:modified><cp:revision>1</cp:revision><dc:subject></dc:subject><dc:title></dc:title>'
            '</cp:coreProperties>'),
        'docProps/app.xml': (
            f'{XML_HEADER}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Template>Normal.dotm'
            '</Template><TotalTime>0</TotalTime><Application>LibreOffice/24.2.7.2$Linux_X86_64 '
            'LibreOffice_project/420$Build-2</Application><AppVersion>15.0000</AppVersion><Pages>1</Pages>'
            '<Words>30</Words><Characters>200</Characters><CharactersWithSpaces>230</CharactersWithSpaces>'
            '<Paragraphs>12</Paragraphs></Properties>'),
        'docProps/custom.xml': (
            f'{XML_HEADER}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"></Properties>'),
        'word/_rels/document.xml.rels': _rels(relationships),
        'word/document.xml': (f'{XML_HEADER}<w:document {DOCUMENT_NAMESPACES}><w:body>' + ''.join(paragraphs)
                              + section + '</w:body></w:document>'),
        'word/styles.xml': _lo_styles(names, footnotes=footnotes, header_footer=header_footer),
        'word/numbering.xml': _lo_numbering(names),
        'word/fontTable.xml': _lo_font_table(names),
        'word/settings.xml': (
            f'{XML_HEADER}<w:settings xmlns:w="{W_NS}"><w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/>'
            f'<w:autoHyphenation w:val="true"/>{settings_notes}<w:compat><w:compatSetting w:name="compatibilityMode" '
            f'w:uri="http://schemas.microsoft.com/office/word" w:val="{names["compat"]}"/></w:compat></w:settings>'),
        'word/theme/theme1.xml': (
            f'{XML_HEADER}<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">'
            '<a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
            '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F497D"/></a:dk2>'
            '<a:lt2><a:srgbClr val="EEECE1"/></a:lt2><a:accent1><a:srgbClr val="4F81BD"/></a:accent1>'
            '<a:accent2><a:srgbClr val="C0504D"/></a:accent2><a:accent3><a:srgbClr val="9BBB59"/></a:accent3>'
            '<a:accent4><a:srgbClr val="8064A2"/></a:accent4><a:accent5><a:srgbClr val="4BACC6"/></a:accent5>'
            '<a:accent6><a:srgbClr val="F79646"/></a:accent6><a:hlink><a:srgbClr val="0000FF"/></a:hlink>'
            '<a:folHlink><a:srgbClr val="800080"/></a:folHlink></a:clrScheme><a:fontScheme name="Office">'
            '<a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
            '<a:minorFont><a:latin typeface="Cambria"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
            '</a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/>'
            '</a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/>'
            '</a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>'
            '</a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'),
        '[Content_Types].xml': _content_types(overrides),
    })
    return _package(parts)


# --- Google Docs -----------------------------------------------------------------------------------------

GOOGLE_NAMESPACES = (
    f'xmlns:w="{W_NS}" xmlns:r="{R_NS}" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:w10="urn:schemas-microsoft-com:office:word" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
)
GOOGLE_FONTS = '<w:rFonts w:ascii="Arial" w:cs="Arial" w:eastAsia="Arial" w:hAnsi="Arial"/>'


def _g_run(text, properties=''):
    """Przebieg jak w Google Docs: własne rPr z w:rtl w:val="0" i kolorem 000000."""
    return f'<w:r><w:rPr>{properties}<w:color w:val="000000"/><w:rtl w:val="0"/></w:rPr>{_t(text)}</w:r>'


def _g_paragraph(properties, runs, *, bookmark=None, index=0):
    """Akapit jak w Google Docs: w:pPr z w:rPr pustym, nagłówki z zakładką _heading=h.… na początku."""
    marks = ''
    if bookmark:
        marks = (f'<w:bookmarkStart w:colFirst="0" w:colLast="0" w:name="_heading={bookmark}" w:id="{index}"/>'
                 f'<w:bookmarkEnd w:id="{index}"/>')
    return f'<w:p><w:pPr>{properties}<w:rPr/></w:pPr>{marks}{runs}</w:p>'


def google_docs_docx(*, footnotes=False, lists=True, hyperlink=True):
    """DOCX „jak z eksportu Google Docs” (Plik → Pobierz → Microsoft Word), strumień na początku.

    Cechy według znajomości eksportu Google Docs (w repo nie ma próbki):

    * brak ``docProps/app.xml``; ``_rels/.rels`` tylko z dokumentem i właściwościami; ``[Content_Types]``
      bez Override dla części relacji;
    * ``styles.xml``: ``w:docDefaults`` minimalny (``w:rFonts`` Arial, ``w:sz 22``, ``w:lang``) i ``w:pPrDefault``
      ze ``w:spacing w:lineRule="auto" w:line="276"``; style ``Normal`` (``w:default="1"``), ``Heading1``…,
      ``Title``, ``Subtitle`` z ``w:name`` pisanym małymi literami („normal”, „heading 1”) – python-docx
      przekłada je na nazwy Worda; nagłówki z ``w:keepNext w:val="1"`` i ``w:keepLines w:val="1"``;
      bez stylów list – pozycja listy to ``Normal`` z ``w:numPr`` i ``w:ind w:left="720" w:hanging="360"``;
    * wartości logiczne jako ``w:val="1"``/``"0"``; każdy przebieg ma ``w:color w:val="000000"`` i
      ``w:rtl w:val="0"``; podkreślenie ``w:u w:val="single"``;
    * ``w:jc`` ``left``/``right``/``center``/``both``; nagłówek dostaje ``w:bookmarkStart`` o nazwie
      ``_heading=h.xxxx``; brak ``w:proofErr`` i ``w:rsid``;
    * ``numbering.xml``: ``w:abstractNum`` z ``w:lvlText w:val="●"`` (nie Symbol z PUA), czcionka
      „Noto Sans Symbols”, ``w:u w:val="none"`` i ``w:vertAlign baseline`` w ``w:rPr`` poziomu;
    * hiperłącze ``w:hyperlink r:id`` (relacja ``TargetMode="External"``) z kolorem 1155cc;
    * ``settings.xml`` z ``w:displayBackgroundShape``, ``w:defaultTabStop 720`` i ``compatibilityMode 15``;
    * ``w:sectPr`` z ``w:pgSz w:w="11909" w:h="16834"`` (A4 w zaokrągleniu Google), marginesami 1440,
      ``w:pgNumType w:start="1"`` i ``w:cols w:space="720" w:num="1"``;
    * przypisy: separatory jak w Wordzie (``w:id`` -1 i 0), pierwszy przypis ``w:id`` 1, odsyłacz z
      ``w:vertAlign superscript`` wprost w ``w:rPr`` (bez stylu odsyłacza).
    """
    paragraphs = [_g_paragraph(
        '<w:pStyle w:val="Heading1"/><w:keepNext w:val="1"/><w:keepLines w:val="1"/>'
        '<w:spacing w:before="400" w:after="120"/>', _g_run(HEADING), bookmark='h.8x1gb5e8l9z3')]
    for key, text in ALIGNED.items():
        paragraphs.append(_g_paragraph(f'<w:jc w:val="{key}"/>', _g_run(text)))
    paragraphs.append(_g_paragraph('', _g_run(BOLD, '<w:b w:val="1"/>') + _g_run(ITALIC, '<w:i w:val="1"/>')
                                   + _g_run(UNDERLINE, '<w:u w:val="single"/>')))
    paragraphs.append(_g_paragraph(
        '<w:tabs><w:tab w:val="right" w:pos="2835" w:leader="dot"/></w:tabs>',
        f'<w:r><w:rPr><w:rtl w:val="0"/></w:rPr>{_t(TAB_BEFORE)}<w:tab/>{_t(TAB_AFTER)}</w:r>'))
    if lists:
        for number, text in ((1, BULLET), (2, NUMBER)):
            paragraphs.append(_g_paragraph(
                f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{number}"/></w:numPr>'
                '<w:ind w:left="720" w:hanging="360"/><w:rPr><w:u w:val="none"/></w:rPr>', _g_run(text)))
    reference = ('<w:r><w:rPr><w:vertAlign w:val="superscript"/><w:rtl w:val="0"/></w:rPr>'
                 '<w:footnoteReference w:id="1"/></w:r>' if footnotes else '')
    paragraphs.append(_g_paragraph('', _g_run(NOTED) + reference))
    paragraphs.append(_g_paragraph(
        '', f'<w:r><w:rPr><w:rtl w:val="0"/></w:rPr>{_t(SOFT_FIRST)}<w:br/>{_t(SOFT_SECOND)}</w:r>'))
    link = ''
    if hyperlink:
        link = (_g_run(' ') + '<w:hyperlink r:id="rId5"><w:r><w:rPr><w:color w:val="1155cc"/><w:u w:val="single"/>'
                f'<w:rtl w:val="0"/></w:rPr>{_t(LINK_TEXT)}</w:r></w:hyperlink>')
    paragraphs.append(_g_paragraph('<w:spacing w:after="0" w:line="240" w:lineRule="auto"/>', _g_run(LAST) + link))
    relationships = [
        ('rId1', f'{R_NS}/styles', 'styles.xml'),
        ('rId2', f'{R_NS}/settings', 'settings.xml'),
        ('rId3', f'{R_NS}/numbering', 'numbering.xml'),
        ('rId4', f'{R_NS}/fontTable', 'fontTable.xml'),
    ]
    overrides = [
        ('/docProps/core.xml', 'application/vnd.openxmlformats-package.core-properties+xml'),
        ('/word/document.xml', WORD_PART.format('document.main')),
        ('/word/styles.xml', WORD_PART.format('styles')),
        ('/word/settings.xml', WORD_PART.format('settings')),
        ('/word/numbering.xml', WORD_PART.format('numbering')),
        ('/word/fontTable.xml', WORD_PART.format('fontTable')),
    ]
    parts = {}
    settings_notes = ''
    if hyperlink:
        relationships.append(('rId5', f'{R_NS}/hyperlink', 'https://example.test/', 'External'))
    if footnotes:
        relationships.append(('rId6', f'{R_NS}/footnotes', 'footnotes.xml'))
        overrides.append(('/word/footnotes.xml', WORD_PART.format('footnotes')))
        parts['word/footnotes.xml'] = (
            f'{XML_HEADER}<w:footnotes {GOOGLE_NAMESPACES}>'
            '<w:footnote w:id="-1" w:type="separator"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
            'w:lineRule="auto"/></w:pPr><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:id="0" w:type="continuationSeparator"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
            'w:lineRule="auto"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
            '<w:footnote w:id="1"><w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:rPr/></w:pPr>'
            '<w:r><w:rPr><w:vertAlign w:val="superscript"/><w:rtl w:val="0"/></w:rPr><w:footnoteRef/></w:r>'
            + _g_run(' ' + NOTE_TEXT, '<w:sz w:val="20"/><w:szCs w:val="20"/>') + '</w:p></w:footnote></w:footnotes>')
        settings_notes = '<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>'
    section = ('<w:sectPr><w:pgSz w:w="11909" w:h="16834"/><w:pgMar w:bottom="1440" w:top="1440" w:left="1440" '
               'w:right="1440" w:header="720" w:footer="720"/><w:pgNumType w:start="1"/>'
               '<w:cols w:space="720" w:num="1"/></w:sectPr>')
    level_font = ('<w:rPr><w:u w:val="none"/><w:rFonts w:ascii="Noto Sans Symbols" w:cs="Noto Sans Symbols" '
                  'w:eastAsia="Noto Sans Symbols" w:hAnsi="Noto Sans Symbols"/><w:vertAlign w:val="baseline"/></w:rPr>')

    def levels(bullet):
        return ''.join(
            f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{"bullet" if bullet else "decimal"}"/>'
            f'<w:lvlText w:val="{"●" if bullet else f"%{i + 1}."}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{720 * (i + 1)}" w:hanging="360"/></w:pPr>'
            + (level_font if bullet else '<w:rPr><w:u w:val="none"/><w:vertAlign w:val="baseline"/></w:rPr>')
            + '</w:lvl>' for i in range(9))

    def style(kind, style_id, name, properties='', runs='', default=False):
        flag = ' w:default="1"' if default else ''
        return (f'<w:style w:type="{kind}" w:styleId="{style_id}"{flag}><w:name w:val="{name}"/>'
                + (f'<w:pPr>{properties}</w:pPr>' if properties or kind == 'paragraph' else '')
                + (f'<w:rPr>{runs}</w:rPr>' if runs else '') + '</w:style>')

    heading = '<w:keepNext w:val="1"/><w:keepLines w:val="1"/><w:spacing w:before="400" w:after="120"/>'
    parts.update({
        '_rels/.rels': _rels([
            ('rId1', f'{R_NS}/officeDocument', 'word/document.xml'),
            ('rId2', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties',
             'docProps/core.xml'),
        ]),
        'docProps/core.xml': (
            f'{XML_HEADER}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
            'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>Tekst</dc:title></cp:coreProperties>'),
        'word/_rels/document.xml.rels': _rels(relationships),
        'word/document.xml': (f'{XML_HEADER}<w:document {GOOGLE_NAMESPACES}><w:body>' + ''.join(paragraphs) + section
                              + '</w:body></w:document>'),
        'word/styles.xml': (
            f'{XML_HEADER}<w:styles {GOOGLE_NAMESPACES}><w:docDefaults><w:rPrDefault><w:rPr>{GOOGLE_FONTS}'
            '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="pl-PL" w:eastAsia="pl-PL" w:bidi="ar-SA"/>'
            '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="0" w:before="0" w:line="276" '
            'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
            + style('paragraph', 'Normal', 'normal', default=True)
            + style('table', 'TableNormal', 'Table Normal', default=True)
            + style('paragraph', 'Heading1', 'heading 1', heading, f'{GOOGLE_FONTS}<w:b w:val="1"/><w:sz w:val="40"/>'
                    '<w:szCs w:val="40"/>')
            + style('paragraph', 'Heading2', 'heading 2', heading, f'{GOOGLE_FONTS}<w:b w:val="1"/><w:sz w:val="32"/>'
                    '<w:szCs w:val="32"/>')
            + style('paragraph', 'Title', 'Title', heading, f'{GOOGLE_FONTS}<w:sz w:val="52"/><w:szCs w:val="52"/>')
            + style('paragraph', 'Subtitle', 'Subtitle', heading,
                    f'{GOOGLE_FONTS}<w:color w:val="666666"/><w:sz w:val="30"/><w:szCs w:val="30"/>')
            + '</w:styles>'),
        'word/settings.xml': (
            f'{XML_HEADER}<w:settings {GOOGLE_NAMESPACES}><w:displayBackgroundShape w:val="1"/>'
            f'<w:defaultTabStop w:val="720"/>{settings_notes}<w:compat><w:compatSetting w:name="compatibilityMode" '
            'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'),
        'word/numbering.xml': (
            f'{XML_HEADER}<w:numbering {GOOGLE_NAMESPACES}>'
            f'<w:abstractNum w:abstractNumId="0">{levels(True)}</w:abstractNum>'
            f'<w:abstractNum w:abstractNumId="1">{levels(False)}</w:abstractNum>'
            '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/>'
            '</w:num></w:numbering>'),
        'word/fontTable.xml': (
            f'{XML_HEADER}<w:fonts {GOOGLE_NAMESPACES}><w:font w:name="Arial"/><w:font w:name="Noto Sans Symbols"/>'
            '</w:fonts>'),
        '[Content_Types].xml': _content_types(overrides, package_rels=False),
    })
    return _package(parts)
