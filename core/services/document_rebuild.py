"""Recreate supported prose using new DOCX objects, never copying source parts."""
from io import BytesIO
from collections import Counter
from copy import deepcopy
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import XmlPart
from docx.oxml import OxmlElement, parse_xml
from docx.shared import RGBColor
from lxml import etree
from docx import Document
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_BREAK

class RebuildUnsupported(ValueError):
    def __init__(self, omissions):
        self.omissions = [f"{label} (liczba: {count})" for label, count in sorted(omissions.items())]
        super().__init__("; ".join(self.omissions))


THEME_FONT_OMISSION = "nierozpoznany krój czcionki z motywu (zastosujemy domyślny)"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
# Znaczniki bez treści (zakładki, zakresy komentarzy, ślady sprawdzania pisowni) – także między akapitami.
CONTENT_FREE = {
    qn("w:" + tag)
    for tag in (
        "bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd",
        "commentRangeStart", "commentRangeEnd", "moveFromRangeStart", "moveFromRangeEnd",
        "moveToRangeStart", "moveToRangeEnd", "customXmlInsRangeStart", "customXmlInsRangeEnd",
        "customXmlDelRangeStart", "customXmlDelRangeEnd", "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
    )
}
# Zapis poprzedniego formatowania przy śledzonych zmianach; przyjęcie zmian go usuwa.
FORMAT_HISTORY = tuple(
    qn("w:" + tag)
    for tag in (
        "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "tblPrExChange",
        "tblGridChange", "trPrChange", "tcPrChange", "numberingChange",
    )
)


MISSING = object()  # właściwość nieokreślona na tym poziomie: decyduje kolejny (styl, ustawienia domyślne)


class Omissions(Counter):
    def add(self, label):
        self[label] += 1


def _hex_byte(value):
    """Wartość themeShade/themeTint (00–FF); uszkodzona jest pomijana jak w Wordzie."""
    try:
        number = int(value, 16) if value else None
    except ValueError:
        return None
    return number if number is not None and 0 <= number <= 255 else None


def _format_resolver(original, omissions):
    # Resolve document defaults and theme fonts/colors without copying source parts.
    default_p = OxmlElement("w:p")
    default_r = OxmlElement("w:r")
    defaults = original.styles.element.find(qn("w:docDefaults"))
    if defaults is not None:
        for path, target in (("w:rPrDefault/w:rPr", default_r), ("w:pPrDefault/w:pPr", default_p)):
            node = defaults
            for part in path.split("/"):
                node = node.find(qn(part)) if node is not None else None
            if node is not None:
                target.append(deepcopy(node))
    default_font = Run(default_r, Paragraph(default_p, original)).font
    default_format = Paragraph(default_p, original).paragraph_format
    theme = None
    for rel in original.part.rels.values():
        if rel.reltype.endswith("/theme") and not rel.is_external:
            theme = etree.fromstring(
                rel.target_part.blob, etree.XMLParser(resolve_entities=False, no_network=True)
            )
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}

    def theme_font(key):
        if theme is None:
            return None
        family = "majorFont" if key.startswith("major") else "minorFont"
        nodes = theme.findall(".//a:fontScheme/a:" + family + "/a:latin", ns)
        return nodes[0].get("typeface") if nodes else None

    def font_name(fonts):
        for font in fonts:
            props = font._element.rPr
            node = props.find(qn("w:rFonts")) if props is not None else None
            if node is not None:
                key = node.get(qn("w:asciiTheme")) or node.get(qn("w:hAnsiTheme"))
                if key:
                    value = theme_font(key)
                    if value:
                        return value
                    # Brak czcionki motywu dotyczy całego dokumentu: jedna uwaga, nie jedna na fragment.
                    omissions[THEME_FONT_OMISSION] = 1
                value = node.get(qn("w:ascii")) or node.get(qn("w:hAnsi"))
                if value:
                    return value
        return None

    def font_color(fonts):
        for font in fonts:
            props = font._element.rPr
            node = props.find(qn("w:color")) if props is not None else None
            if node is None:
                continue
            value = node.get(qn("w:val"))
            key = node.get(qn("w:themeColor"))
            if key and key.isalnum() and theme is not None:
                key = {
                    "text1": "dk1",
                    "text2": "dk2",
                    "background1": "lt1",
                    "background2": "lt2",
                }.get(key, key)
                color = theme.find(".//a:clrScheme/a:" + key, ns)
                if color is not None and len(color):
                    value = color[0].get("lastClr") or color[0].get("val")
            if value and len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
                rgb = [int(value[i : i + 2], 16) for i in (0, 2, 4)]
                shade = _hex_byte(node.get(qn("w:themeShade")))
                tint = _hex_byte(node.get(qn("w:themeTint")))
                if shade is not None:
                    rgb = [round(c * shade / 255) for c in rgb]
                if tint is not None:
                    rgb = [round(c + (255 - c) * (1 - tint / 255)) for c in rgb]
                return RGBColor(*rgb)
            if value == "auto":
                return None
        return MISSING

    return default_font, default_format, font_name, font_color


def _unwrap(node, content):
    parent = node.getparent()
    index = parent.index(node)
    for child in list(content) if content is not None else ():
        parent.insert(index, child)
        index += 1
    parent.remove(node)


HYPERLINK_OMISSION = "odnośniki hiperłączy (widoczny tekst pozostanie)"
TOC_OMISSION = "spis treści (wstaw go ponownie w Wordzie: Odwołania → Spis treści)"


PLACEHOLDER_STYLES = frozenset({"PlaceholderText"})
PLACEHOLDER_OMISSION = "oznaczenie tekstu zastępczego kontrolek z wpisaną treścią (treść pozostanie – sprawdź ją)"


def placeholder_styles(document):
    """Identyfikatory stylu „Tekst zastępczy” (w polskim Wordzie np. „Tekstzastpczy”)."""
    ids = set(PLACEHOLDER_STYLES)
    for style in document.styles.element.findall(qn("w:style")):
        name = style.find(qn("w:name"))
        if name is not None and (name.get(qn("w:val")) or "").casefold() == "placeholder text":
            ids.add(style.get(qn("w:styleId")))
    return ids


def _only_placeholder(node, styles):
    """Cała widoczna treść kontrolki ma styl tekstu zastępczego (albo kontrolka jest pusta)."""
    content = node.find(qn("w:sdtContent"))
    if content is None:
        return True
    for text in content.iter(qn("w:t")):
        if not (text.text or "").strip():
            continue
        run = text.getparent()
        style = run.find(qn("w:rPr") + "/" + qn("w:rStyle"))
        if style is None or style.get(qn("w:val")) not in styles:
            return False
    return True


def _unwrap_content_controls(body, omissions, styles=PLACEHOLDER_STYLES):
    """Kontrolki zawartości (w:sdt) znikają, a ich treść z formatowaniem zostaje na swoim miejscu.

    Dotyczy kontrolek blokowych, w akapicie, w wierszach i komórkach tabel. Kontrolka pokazująca tekst
    zastępczy („Kliknij tutaj…”) znika razem z nim – ale tylko wtedy, gdy cała jej treść ma styl tekstu
    zastępczego i nie ma w niej innych kontrolek. Kontrolka z oznaczeniem w:showingPlcHdr, w którą wpisano
    treść (np. program ją wypełnił albo grupa z wypełnionymi polami), jest rozpakowana z uwagą.
    Spis treści z galerii Worda (w:docPartObj) usuwamy w całości: jego numery stron nie pasowałyby
    do nowego dokumentu.
    """
    controls = list(body.iter(qn("w:sdt")))
    # Przed rozpakowaniem: kontrolki zawierające inne kontrolki (np. grupa pól).
    nested = {id(node) for node in controls if any(True for _ in node.iterdescendants(qn("w:sdt")))}
    # Od końca: zagnieżdżona kontrolka jest rozpakowana, zanim zajmiemy się zewnętrzną.
    for node in reversed(controls):
        parent = node.getparent()
        if parent is None:
            continue
        properties = node.find(qn("w:sdtPr"))
        # Elementy kontrolki nie mają klas python-docx (xpath bez prefiksów), więc szukamy przez find.
        gallery = node.find("/".join(qn(tag) for tag in ("w:sdtPr", "w:docPartObj", "w:docPartGallery")))
        if gallery is not None and (gallery.get(qn("w:val")) or "").startswith("Table of Contents"):
            omissions.add(TOC_OMISSION)
            parent.remove(node)
        elif properties is not None and properties.find(qn("w:showingPlcHdr")) is not None:
            if id(node) not in nested and _only_placeholder(node, styles):
                parent.remove(node)
            else:
                omissions.add(PLACEHOLDER_OMISSION)
                _unwrap(node, node.find(qn("w:sdtContent")))
        else:
            _unwrap(node, node.find(qn("w:sdtContent")))


def _unwrap_simple_fields(body, omissions, label):
    """Pole proste (w:fldSimple) zostawia swój ostatni wynik jako zwykły tekst – jak pole złożone."""
    fields = list(body.iter(qn("w:fldSimple")))
    for node in fields:
        if not any(parent.tag == node.tag for parent in node.iterancestors()):
            omissions.add(label)
    for node in fields:
        for data in node.findall(qn("w:fldData")):
            node.remove(data)
        _unwrap(node, node)


def _sanitize_body(body, omissions, styles=PLACEHOLDER_STYLES):
    # Treść alternatywna (mc:AlternateContent): bierzemy wersję zapasową dla prostszych programów,
    # a bez niej pierwszy wariant. Inaczej pole tekstowe liczyło się podwójnie (DrawingML + VML).
    for node in list(body.iter(MC + "AlternateContent")):
        if node.getparent() is not None:
            _unwrap(node, node.find(MC + "Fallback") if node.find(MC + "Fallback") is not None
                    else node.find(MC + "Choice"))
    _unwrap_content_controls(body, omissions, styles)
    for node in list(body.iter(*FORMAT_HISTORY)):
        if node.getparent() is not None:
            node.getparent().remove(node)
    # Accept inline revisions: retained runs lose reviewer IDs and timestamps.
    for tag in ("del", "moveFrom"):
        for node in list(body.iter(qn("w:" + tag))):
            if node.getparent() is not None:
                if node.getparent().tag == qn("w:rPr"):
                    omissions.add("oznaczenia usunięcia akapitu (akapit pozostanie)")
                node.getparent().remove(node)
    for tag in ("ins", "moveTo"):
        for node in list(body.iter(qn("w:" + tag))):
            parent = node.getparent()
            if parent is not None:
                if parent.tag in (qn("w:rPr"), qn("w:pPr")):
                    omissions.add("oznaczenia zmian akapitów (podział na akapity pozostanie)")
                index = parent.index(node)
                for child in list(node):
                    parent.insert(index, child)
                    index += 1
                parent.remove(node)
    unsupported = {
        "tbl": "tabele wraz z ich treścią",
        "drawing": "ilustracje i pola tekstowe wraz z ich treścią",
        "pict": "ilustracje i pola tekstowe wraz z ich treścią",
        "object": "obiekty osadzone",
        "fldChar": "mechanizm pól automatycznych (zwykły tekst wyniku może pozostać)",
        # Instrukcja jest częścią tego samego pola (liczonego przy fldChar), a nie osobną pozycją.
        "instrText": None,
        "altChunk": "dołączona treść zewnętrzna",
        "sym": "symbole specjalne zapisane jako obiekty",
    }
    for tag, label in unsupported.items():
        for node in list(body.iter(qn("w:" + tag))):
            # Jedna tabela czy ilustracja to jedna pozycja, także gdy zawiera zagnieżdżone; pole – jego początek.
            counted = label and not any(parent.tag == node.tag for parent in node.iterancestors())
            if tag == "fldChar":
                counted = node.get(qn("w:fldCharType")) == "begin"
            if counted:
                omissions.add(label)
            if node.getparent() is not None:
                node.getparent().remove(node)
    # Po usunięciu tabel i ilustracji: pole proste w nich znika razem z nimi i nie jest liczone.
    _unwrap_simple_fields(body, omissions, unsupported["fldChar"])
    math = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    for node in list(body.iter()):
        parent = node.getparent()
        # Jedno równanie to jeden element najwyższego poziomu; jego części nie są liczone osobno.
        if node.tag.startswith(math) and parent is not None and not parent.tag.startswith(math):
            omissions.add("równania matematyczne")
            parent.remove(node)
    for paragraph in body.iter(qn("w:p")):
        for path, label in (
            ("./w:pPr/w:pBdr", "obramowania akapitów"),
            ("./w:pPr/w:shd", "cieniowanie akapitów"),
            ("./w:pPr/w:framePr", "ramki akapitów"),
            ("./w:pPr/w:textDirection", "niestandardowy kierunek tekstu"),
            (".//w:rPr/w:spacing", "rozstrzelenie znaków"),
            (".//w:rPr/w:shd", "cieniowanie znaków"),
            (".//w:rPr/w:bdr", "obramowania znaków"),
        ):
            for _ in paragraph.xpath(path):
                omissions.add(label)


LANGUAGE_ATTRIBUTES = ("val", "eastAsia", "bidi")


def _document_language(original):
    """Atrybuty w:lang z ustawień domyślnych dokumentu (np. pl-PL) – słownik i dzielenie wyrazów."""
    nodes = original.styles.element.xpath("./w:docDefaults/w:rPrDefault/w:rPr/w:lang")
    if not nodes:
        return {}
    return {name: nodes[0].get(qn("w:" + name)) for name in LANGUAGE_ATTRIBUTES if nodes[0].get(qn("w:" + name))}


def _language(elements):
    """Język tekstu łacińskiego: fragment, potem styl znakowy i styl akapitu."""
    for element in elements:
        values = element.xpath("./w:rPr/w:lang/@w:val")
        if values:
            return values[0]
    return None


def _numbered(elements):
    """Numeracja z akapitu albo stylu; numId 0 oznacza jej wyłączenie."""
    for element in elements:
        values = element.xpath("./w:pPr/w:numPr/w:numId/@w:val")
        if values:
            return values[0] != "0"
    return False


def styles(style):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        yield style
        style = style.base_style


def effective(objects, name):
    for obj in objects:
        value = getattr(obj, name)
        if value is not None:
            return value
    return None


PARAGRAPH_FORMAT = (
    "alignment",
    "first_line_indent",
    "left_indent",
    "right_indent",
    "space_before",
    "space_after",
    "line_spacing",
    "line_spacing_rule",
    "keep_together",
    "keep_with_next",
    "page_break_before",
    "widow_control",
)
RUN_FORMAT = (
    "size",
    "bold",
    "italic",
    "underline",
    "strike",
    "double_strike",
    "superscript",
    "subscript",
    "small_caps",
    "all_caps",
    "highlight_color",
)
BREAKS = {"page": WD_BREAK.PAGE, "column": WD_BREAK.COLUMN}


def _tab_stops(paragraph_format):
    return [(stop.position, stop.alignment, stop.leader) for stop in paragraph_format.tab_stops]


class _StyleChains:
    """Formatowanie wynikające ze stylów i ustawień domyślnych, liczone raz na styl.

    Fragment dziedziczy po: własnym formatowaniu, stylu znakowym (z jego bazowymi), stylu akapitu
    (z bazowymi) i ustawieniach domyślnych dokumentu. Stylów jest kilkadziesiąt, fragmentów –
    dziesiątki tysięcy, więc część wspólną liczymy raz, a dla fragmentu czytamy tylko jego własny w:rPr."""

    def __init__(self, original, omissions):
        self.part = original.part
        self.default_font, self.default_format, self.font_name, self.font_color = _format_resolver(
            original, omissions
        )
        self._styles = {}
        self._paragraphs = {}
        self._runs = {}

    def _style(self, style_id, kind):
        key = (style_id, kind)
        if key not in self._styles:
            self._styles[key] = list(styles(self.part.get_style(style_id, kind)))
        return self._styles[key]

    def paragraph(self, style_id):
        if style_id not in self._paragraphs:
            chain = self._style(style_id, WD_STYLE_TYPE.PARAGRAPH)
            formats = [s.paragraph_format for s in chain] + [self.default_format]
            fonts = [s.font for s in chain] + [self.default_font]
            tabs = next((_tab_stops(f) for f in formats if len(f.tab_stops)), [])
            self._paragraphs[style_id] = {
                "styles": chain,
                "numbered": _numbered([s.element for s in chain]),
                "heading": next(
                    (s.name for s in chain if s.name.startswith("Heading ") and s.name[8:].isdigit()),
                    None,
                ),
                "format": {name: effective(formats, name) for name in PARAGRAPH_FORMAT},
                "tabs": tabs,
                "mark_size": effective(fonts, "size"),
                "mark_name": self.font_name(fonts),
            }
        return self._paragraphs[style_id]

    def run(self, run_style_id, paragraph_style_id):
        key = (run_style_id, paragraph_style_id)
        if key not in self._runs:
            chain = self._style(run_style_id, WD_STYLE_TYPE.CHARACTER) + self.paragraph(
                paragraph_style_id
            )["styles"]
            fonts = [s.font for s in chain] + [self.default_font]
            color = self.font_color(fonts)
            self._runs[key] = {
                "format": {name: effective(fonts, name) for name in RUN_FORMAT + ("hidden",)},
                "name": self.font_name(fonts),
                "color": None if color is MISSING else color,
                "lang": _language([s.element for s in chain]),
            }
        return self._runs[key]


def _style_id(properties, tag):
    node = properties.find(qn(tag)) if properties is not None else None
    return node.get(qn("w:val")) if node is not None else None


NOTE_KINDS = ("footnote", "endnote")
NOTE_REFERENCE_TAGS = {qn("w:" + kind + "Reference"): kind for kind in NOTE_KINDS}
NOTE_MARK_TAGS = frozenset(qn("w:" + kind + "Ref") for kind in NOTE_KINDS)
NOTE_MISSING_OMISSION = "odsyłacze do nieistniejących przypisów"
# Style Worda (identyfikator, nazwa) tekstu przypisu i odsyłacza.
NOTE_STYLES = {
    "footnote": (("FootnoteText", "footnote text"), ("FootnoteReference", "footnote reference")),
    "endnote": (("EndnoteText", "endnote text"), ("EndnoteReference", "endnote reference")),
}
NOTE_RELATIONSHIPS = {"footnote": RT.FOOTNOTES, "endnote": RT.ENDNOTES}


class _SourceNotes:
    """Przypisy dolne i końcowe źródła, odtwarzane w kolejności odsyłaczy (nowe w:id od 1).

    Treść przypisu przechodzi tę samą sanityzację i to samo odtwarzanie akapitów co treść główna,
    a pominięcia w niej liczą się razem z resztą. Przypis bez odsyłacza w tekście znika bez uwagi;
    odsyłacz do nieistniejącego przypisu znika z uwagą. Odsyłacz z własnym znakiem (customMarkFollows)
    zachowuje ten atrybut – sam znak to zwykły tekst za odsyłaczem i zostaje jak każdy tekst."""

    def __init__(self, original, omissions, placeholders):
        self.omissions = omissions
        self.placeholders = placeholders
        self.source = {kind: {} for kind in NOTE_KINDS}
        for rel in original.part.rels.values():
            for kind in NOTE_KINDS:
                if not rel.is_external and rel.reltype == NOTE_RELATIONSHIPS[kind]:
                    for note in parse_xml(rel.target_part.blob).findall(qn("w:" + kind)):
                        if note.get(qn("w:type"), "normal") == "normal":
                            self.source[kind].setdefault(note.get(qn("w:id")), note)
        self.notes = {kind: [] for kind in NOTE_KINDS}
        self._built = {}

    def reference(self, node, build):
        """Nowy przypis (rodzaj, w:id, własny znak) dla odsyłacza albo None, gdy przypisu brak."""
        kind = NOTE_REFERENCE_TAGS[node.tag]
        key = (kind, node.get(qn("w:id")))
        note = self.source[kind].get(key[1])
        if note is None:
            self.omissions.add(NOTE_MISSING_OMISSION)
            return None
        custom = node.get(qn("w:customMarkFollows")) in ("1", "true", "on")
        if key not in self._built:
            _sanitize_body(note, self.omissions, self.placeholders)
            for _ in note.iter(qn("w:hyperlink")):
                self.omissions.add(HYPERLINK_OMISSION)
            marks = []
            paragraphs = build(note, marks) or [{"heading": None, "format": {}, "tabs": [], "runs": [], "mark": {}}]
            for paragraph in paragraphs:
                paragraph["heading"] = None
                paragraph["style"] = NOTE_STYLES[kind][0][0]
            # Znak przypisu zawsze na początku treści; przy własnym znaku tylko wtedy, gdy był w źródle.
            if marks or not custom:
                paragraphs[0]["runs"].insert(0, {**(marks[0] if marks else {"font": {}}), "content": [("mark", kind)]})
            self._built[key] = paragraphs
        self.notes[kind].append(self._built[key])
        return kind, len(self.notes[kind]), custom


def _paragraph_specs(original, omissions, notes):
    """Opisy akapitów treści głównej; odsyłacze przypisów (notes) budują też opisy treści przypisów."""
    chains = _StyleChains(original, omissions)
    font_name, font_color = chains.font_name, chains.font_color
    document_language = _document_language(original).get("val")

    def blocks(container, marks=None):
        """Akapity kontenera (treść albo przypis); marks zbiera formatowanie znaku przypisu."""
        paragraphs = []
        for element in container:
            if element.tag == qn("w:sectPr"):
                continue
            if element.tag in CONTENT_FREE:
                continue
            if element.tag != qn("w:p"):
                omissions.add("blok " + etree.QName(element).localname + " wraz z treścią")
                continue
            paragraphs.append(paragraph_spec(element, marks))
        return paragraphs

    def paragraph_spec(element, marks):
        old = Paragraph(element, original)
        properties = element.pPr
        paragraph_style_id = _style_id(properties, "w:pStyle")
        inherited = chains.paragraph(paragraph_style_id)
        direct_numbering = element.xpath("./w:pPr/w:numPr/w:numId/@w:val")
        if direct_numbering[0] != "0" if direct_numbering else inherited["numbered"]:
            omissions.add("automatyczne numerowanie i punktory list (tekst pozycji pozostanie)")
        paragraph = {"heading": inherited["heading"], "format": {}, "tabs": [], "runs": [], "mark": {}}
        # Znak akapitu wyznacza wysokość pustego akapitu (np. odstępu między scenami).
        mark_properties = properties.find(qn("w:rPr")) if properties is not None else None
        mark_size, mark_name = inherited["mark_size"], inherited["mark_name"]
        if mark_properties is not None:
            mark = OxmlElement("w:r")
            mark.append(deepcopy(mark_properties))
            mark_font = Run(mark, old).font
            mark_size = mark_font.size if mark_font.size is not None else mark_size
            mark_name = font_name([mark_font]) or mark_name
        if mark_size is not None:
            paragraph["mark"]["size"] = mark_size
        if mark_name:
            paragraph["mark"]["name"] = mark_name
        own = old.paragraph_format if properties is not None else None
        for name in PARAGRAPH_FORMAT:
            value = getattr(own, name) if own is not None else None
            if value is None:
                value = inherited["format"][name]
            if value is not None:
                paragraph["format"][name] = value
        paragraph["tabs"] = (
            _tab_stops(own) if own is not None and len(own.tab_stops) else list(inherited["tabs"])
        )
        runs = paragraph["runs"]
        for element_run in element.iter(qn("w:r")):
            run_properties = element_run.rPr
            shared = chains.run(_style_id(run_properties, "w:rStyle"), paragraph_style_id)
            font = Run(element_run, old).font if run_properties is not None else None
            hidden = font.hidden if font is not None else None
            if hidden if hidden is not None else shared["format"]["hidden"]:
                continue
            formatting = {"font": {}}
            for name in RUN_FORMAT:
                value = getattr(font, name) if font is not None else None
                if value is None:
                    value = shared["format"][name]
                if value is not None:
                    formatting["font"][name] = value
            name = (font_name([font]) if font is not None else None) or shared["name"]
            if name:
                formatting["font"]["name"] = name
            color = font_color([font]) if font is not None else MISSING
            color = shared["color"] if color is MISSING else color
            if color is not None:
                formatting["color"] = color
            language = _language([element_run]) or shared["lang"]
            if language and language != document_language:
                formatting["lang"] = language
            # Odsyłacz i znak przypisu dostają własny przebieg (styl odsyłacza nie obejmie tekstu obok);
            # tekst za nimi trafia do kolejnego przebiegu o tym samym formatowaniu.
            new_run = {**formatting, "content": []}
            runs.append(new_run)
            for child in element_run:
                tag = child.tag
                if tag == qn("w:rPr"):
                    continue
                if tag in NOTE_MARK_TAGS and marks is not None:
                    marks.append(formatting)
                    if new_run is not None and not new_run["content"]:
                        runs.pop()
                    new_run = None
                    continue
                if tag in NOTE_REFERENCE_TAGS and marks is None:
                    reference = notes.reference(child, blocks)
                    if reference is not None:
                        if new_run is not None and not new_run["content"]:
                            runs.pop()
                        runs.append({**formatting, "content": [("note", reference)]})
                        new_run = None
                    continue
                if new_run is None:
                    new_run = {**formatting, "content": []}
                    runs.append(new_run)
                if tag == qn("w:t"):
                    new_run["content"].append(("text", child.text or ""))
                elif tag == qn("w:tab"):
                    new_run["content"].append(("tab", None))
                elif tag in (qn("w:br"), qn("w:cr")):
                    kind = child.get(qn("w:type"), "textWrapping")
                    new_run["content"].append(("break", BREAKS.get(kind, WD_BREAK.LINE)))
                elif tag == qn("w:noBreakHyphen"):
                    new_run["content"].append(("text", "‑"))
                elif tag == qn("w:softHyphen"):
                    new_run["content"].append(("text", "­"))
                elif tag in (qn("w:commentReference"), qn("w:lastRenderedPageBreak")):
                    continue
                else:
                    omissions.add("element " + etree.QName(child).localname + " wewnątrz tekstu")
        return paragraph

    return blocks(original.element.body)


PAGE_DIMENSIONS = (
    "page_width", "page_height", "orientation", "top_margin", "bottom_margin", "left_margin",
    "right_margin", "header_distance", "footer_distance", "gutter",
)
COLUMN_ATTRIBUTES = ("num", "space", "equalWidth", "sep")
# Ustawienia dokumentu dotyczące marginesów (strony lustrzane w książce, margines na oprawę u góry).
PAGE_SETTINGS = ("mirrorMargins", "gutterAtTop")


def _page_format(original):
    """Format strony pierwszej sekcji źródła: wymiary, orientacja, marginesy i kolumny.

    Przenosimy same wartości (liczby i atrybuty), budując nowe elementy – nie kopiujemy części źródła.
    """
    section = original.sections[0]
    columns = section._sectPr.find(qn("w:cols"))
    return {
        "dimensions": {name: getattr(section, name) for name in PAGE_DIMENSIONS},
        "columns": None if columns is None else (
            {name: columns.get(qn("w:" + name)) for name in COLUMN_ATTRIBUTES if columns.get(qn("w:" + name))},
            [
                {name: column.get(qn("w:" + name)) for name in ("w", "space") if column.get(qn("w:" + name))}
                for column in columns.findall(qn("w:col"))
            ],
        ),
        "settings": [
            name for name in PAGE_SETTINGS
            if original.settings.element.xpath("./w:%s[not(@w:val) or not(@w:val='0' or @w:val='false'"
                                               " or @w:val='off')]" % name)
        ],
    }


def _analyze_docx(source, keep_page_format=False):
    source.seek(0)
    original = Document(source)
    body = original.element.body
    omissions = Omissions()
    for rel in original.part.rels.values():
        if not rel.is_external and rel.reltype.endswith(("/header", "/footer")):
            root = etree.fromstring(
                rel.target_part.blob, etree.XMLParser(resolve_entities=False, no_network=True)
            )
            # Znak wodny i grafika w nagłówku nie mają w:t (np. VML v:textpath), a też znikną.
            if any((node.text or "").strip() for node in root.iter(qn("w:t"))) or any(
                True for _ in root.iter(qn("w:drawing"), qn("w:pict"), qn("w:object"))
            ):
                omissions.add("niepuste nagłówki lub stopki")
    placeholders = placeholder_styles(original)
    _sanitize_body(body, omissions, placeholders)
    # Po rozpakowaniu kontrolek zawartości: podział sekcji bywa w akapicie wewnątrz kontrolki.
    if len(original.sections) > 1:
        label = "o układzie pierwszej sekcji" if keep_page_format else "A4"
        omissions[f"podziały na sekcje i ustawienia ich układu (pozostanie jedna sekcja {label})"] = (
            len(original.sections) - 1
        )
    elif not keep_page_format and _custom_page_layout(original.sections[0]):
        # Marginesy nie są zgłaszane (Word i LibreOffice mają różne domyślne);
        # format strony, orientacja i kolumny zmieniają wygląd całej książki.
        omissions.add("format strony, orientacja lub kolumny (wynik: A4 pionowo, jedna kolumna)")
    # Liczymy po usunięciu tabel i pól tekstowych: odnośnik z nich nie zostawia „widocznego tekstu”.
    for link in body.iter(qn("w:hyperlink")):
        omissions.add(HYPERLINK_OMISSION)
    notes = _SourceNotes(original, omissions, placeholders)
    layout = {
        "paragraphs": _paragraph_specs(original, omissions, notes),
        "notes": notes.notes,
        "page": _page_format(original) if keep_page_format else None,
        "language": _document_language(original),
        "default_tab": original.settings.element.xpath("./w:defaultTabStop/@w:val"),
        "defaults": {
            etree.QName(node).localname
            for node in original.styles.element.xpath(
                "./w:docDefaults | ./w:docDefaults/w:rPrDefault | ./w:docDefaults/w:pPrDefault"
            )
        },
    }
    return layout, omissions


def inspect_docx(source, *, keep_page_format=False):
    """Return exactly the rebuild warnings without creating or saving an output DOCX."""
    _, omissions = _analyze_docx(source, keep_page_format)
    if omissions:
        raise RebuildUnsupported(omissions)


def _neutral_styles(result, language, present):
    """Szablon python-docx nie może dokładać własnego wyglądu (10 pt odstępu, 1,15 interlinii, 11 pt,
    Calibri, niebieskich nagłówków): formatowanie źródła przenosimy jawnie, a czego źródło nie
    określało, zostaje przy wartościach bazowych Worda – tak jak w oryginale."""
    styles = result.styles.element
    for node in styles.xpath("./w:docDefaults/w:rPrDefault/w:rPr | ./w:docDefaults/w:pPrDefault/w:pPr"):
        for child in list(node):
            node.remove(child)
    # Programy różnie traktują brak ustawień domyślnych (np. LibreOffice przyjmuje wtedy wartości
    # Worda 2007), więc odtwarzamy też samą obecność tych elementów.
    for name in ("rPrDefault", "pPrDefault"):
        if name not in present:
            for node in styles.xpath("./w:docDefaults/w:" + name):
                node.getparent().remove(node)
    if "docDefaults" not in present:
        for node in styles.xpath("./w:docDefaults"):
            styles.remove(node)
    if language and styles.xpath("./w:docDefaults/w:rPrDefault/w:rPr"):
        properties = styles.xpath("./w:docDefaults/w:rPrDefault/w:rPr")[0]
        node = OxmlElement("w:lang")
        for name, value in language.items():
            node.set(qn("w:" + name), value)
        properties.append(node)
    for style in styles.xpath("./w:style[@w:type='paragraph']"):
        for node in style.xpath("./w:rPr"):
            style.remove(node)
        for node in style.xpath("./w:pPr/*[not(self::w:outlineLvl)]"):
            node.getparent().remove(node)


def _new_document(layout):
    paragraphs = layout["paragraphs"]
    result = Document()
    _neutral_styles(result, layout["language"], layout["defaults"])
    settings = result.settings.element
    for node in settings.xpath("./w:proofState"):
        # Szablon deklaruje „sprawdzono pisownię”; nowy dokument Word ma sprawdzić od nowa.
        settings.remove(node)
    if layout["default_tab"]:
        settings.xpath("./w:defaultTabStop")[0].set(qn("w:val"), layout["default_tab"][0])
    for section in result.sections:
        section.page_width = Cm(21)
        section.page_height = Cm(29.7)
        section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = (
            Cm(2.5)
        )
        if layout["page"]:
            _apply_page_format(result, section, layout["page"])
    heading_styles = {None: None}
    for item in paragraphs:
        # Wyszukanie stylu po nazwie przegląda wszystkie style; robimy to raz na nazwę, nie na akapit.
        heading = item["heading"]
        if heading not in heading_styles:
            heading_styles[heading] = result.styles[heading] if heading in result.styles else None
        _fill_paragraph(result.add_paragraph(style=heading_styles[heading]), item)
    _add_notes(result, layout["notes"])
    props = result.core_properties
    for name in (
        "author",
        "last_modified_by",
        "title",
        "subject",
        "comments",
        "keywords",
        "category",
        "content_status",
        "identifier",
        "language",
        "version",
    ):
        setattr(props, name, "")
    for tag in ("dcterms:created", "dcterms:modified", "cp:lastPrinted"):
        for node in list(props._element.findall(qn(tag))):
            props._element.remove(node)
    props.revision = 1
    return result


def _fill_paragraph(paragraph, item):
    """Formatowanie i przebiegi akapitu według opisu z _paragraph_specs (treść główna i przypisy)."""
    if "style" in item:
        paragraph._p.style = item["style"]
    for name, value in item["format"].items():
        setattr(paragraph.paragraph_format, name, value)
    for stop in item["tabs"]:
        paragraph.paragraph_format.tab_stops.add_tab_stop(*stop)
    if item["mark"]:
        mark = OxmlElement("w:rPr")
        if "name" in item["mark"]:
            fonts = OxmlElement("w:rFonts")
            for key in ("w:ascii", "w:hAnsi"):
                fonts.set(qn(key), item["mark"]["name"])
            mark.append(fonts)
        if "size" in item["mark"]:
            size = OxmlElement("w:sz")
            size.set(qn("w:val"), str(round(item["mark"]["size"].pt * 2)))
            mark.append(size)
        paragraph._p.get_or_add_pPr().append(mark)
    for specification in item["runs"]:
        run = paragraph.add_run()
        for kind, value in specification["content"]:
            if kind == "text":
                run.add_text(value)
            elif kind == "tab":
                run.add_tab()
            elif kind == "note":
                note_kind, number, custom = value
                run._r.style = NOTE_STYLES[note_kind][1][0]
                reference = OxmlElement("w:" + note_kind + "Reference")
                if custom:
                    reference.set(qn("w:customMarkFollows"), "1")
                reference.set(qn("w:id"), str(number))
                run._r.append(reference)
            elif kind == "mark":
                run._r.style = NOTE_STYLES[value][1][0]
                run._r.append(OxmlElement("w:" + value + "Ref"))
            else:
                run.add_break(value)
        for name, value in specification["font"].items():
            setattr(run.font, name, value)
        if "color" in specification:
            run.font.color.rgb = specification["color"]
        if "lang" in specification:
            language = OxmlElement("w:lang")
            language.set(qn("w:val"), specification["lang"])
            run._r.get_or_add_rPr().insert_element_before(
                language, "w:eastAsianLayout", "w:specVanish", "w:oMath"
            )


# Elementy ustawień (schemat w:settings), przed którymi stoją w:footnotePr i w:endnotePr.
SETTINGS_AFTER_NOTES = tuple(
    qn(tag)
    for tag in (
        "w:endnotePr", "w:compat", "w:docVars", "w:rsids", "m:mathPr", "w:attachedSchema", "w:themeFontLang",
        "w:clrSchemeMapping", "w:doNotIncludeSubdocsInStats", "w:doNotAutoCompressPictures", "w:forceUpgrade",
        "w:captions", "w:readModeInkLockDown", "w:smartTagType", "sl:schemaLibrary", "w:shapeDefaults",
        "w:doNotEmbedSmartTags", "w:decimalSymbol", "w:listSeparator",
    )
)


def _add_notes(result, notes):
    """Nowe części przypisów dolnych i końcowych z separatorami jak w Wordzie (w:id -1 i 0).

    Style tekstu przypisu i odsyłacza są neutralne jak pozostałe (_neutral_styles): formatowanie
    źródła niosą akapity i przebiegi, a styl odsyłacza daje tylko indeks górny, jak w Wordzie.
    """
    styles, settings = result.styles.element, result.settings.element
    for kind in NOTE_KINDS:
        if not notes.get(kind):
            continue
        (text_id, text_name), (reference_id, reference_name) = NOTE_STYLES[kind]
        if result.styles.element.get_by_id(text_id) is None:
            styles.append(parse_xml(
                f'<w:style {nsdecls("w")} w:type="paragraph" w:styleId="{text_id}"><w:name w:val="{text_name}"/>'
                '<w:basedOn w:val="Normal"/><w:uiPriority w:val="99"/><w:unhideWhenUsed/></w:style>'))
        if result.styles.element.get_by_id(reference_id) is None:
            styles.append(parse_xml(
                f'<w:style {nsdecls("w")} w:type="character" w:styleId="{reference_id}">'
                f'<w:name w:val="{reference_name}"/><w:basedOn w:val="DefaultParagraphFont"/>'
                '<w:uiPriority w:val="99"/><w:unhideWhenUsed/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
                '</w:style>'))
        separators = "".join(
            f'<w:{kind} w:type="{name}" w:id="{number}"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
            f'w:lineRule="auto"/></w:pPr><w:r><w:{name}/></w:r></w:p></w:{kind}>'
            for number, name in ((-1, "separator"), (0, "continuationSeparator"))
        )
        root = parse_xml(f'<w:{kind}s {nsdecls("w")}>{separators}</w:{kind}s>')
        for number, paragraphs in enumerate(notes[kind], 1):
            note = OxmlElement("w:" + kind)
            note.set(qn("w:id"), str(number))
            root.append(note)
            for item in paragraphs:
                element = OxmlElement("w:p")
                note.append(element)
                _fill_paragraph(Paragraph(element, result._body), item)
        part = XmlPart(
            PackURI(f"/word/{kind}s.xml"),
            f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}s+xml",
            root,
            result.part.package,
        )
        result.part.relate_to(part, NOTE_RELATIONSHIPS[kind])
        properties = parse_xml(
            f'<w:{kind}Pr {nsdecls("w")}><w:{kind} w:id="-1"/><w:{kind} w:id="0"/></w:{kind}Pr>'
        )
        anchor = next((node for node in settings if node.tag in SETTINGS_AFTER_NOTES), None)
        if anchor is None:
            settings.append(properties)
        else:
            anchor.addprevious(properties)


def _apply_page_format(result, section, page):
    """Format strony źródła; czego źródło nie określało, zostaje jak przy zwykłej przebudowie."""
    for name, value in page["dimensions"].items():
        if value is not None:
            setattr(section, name, value)
    if page["columns"] is not None:
        attributes, widths = page["columns"]
        columns = section._sectPr.find(qn("w:cols"))
        if columns is None:
            columns = OxmlElement("w:cols")
            section._sectPr.insert_element_before(columns, "w:formProt", "w:vAlign", "w:noEndnote",
                                                   "w:titlePg", "w:textDirection", "w:bidi",
                                                   "w:rtlGutter", "w:docGrid", "w:printerSettings",
                                                   "w:sectPrChange")
        for key in list(columns.attrib):
            del columns.attrib[key]
        for child in list(columns):
            columns.remove(child)
        for name, value in attributes.items():
            columns.set(qn("w:" + name), value)
        for width in widths:
            column = OxmlElement("w:col")
            for name, value in width.items():
                column.set(qn("w:" + name), value)
            columns.append(column)
    settings = result.settings.element
    anchor = settings.find(qn("w:zoom"))
    index = settings.index(anchor) + 1 if anchor is not None else 0
    for name in page["settings"]:
        settings.insert(index, OxmlElement("w:" + name))
        index += 1


def _custom_page_layout(section):
    """Strona inna niż A4 lub Letter pionowo albo w kilku kolumnach (tolerancja 2 mm).

    Letter (domyślny w angielskim Wordzie) zamieniony na A4 nie zmienia układu książki,
    więc nie wymaga potwierdzenia; A5, B5, własny format książkowy i orientacja pozioma tak.
    """
    width, height = section.page_width, section.page_height
    if width is not None and height is not None and not any(
        abs(width - Cm(w)) <= Cm(0.2) and abs(height - Cm(h)) <= Cm(0.2)
        for w, h in ((21, 29.7), (21.59, 27.94))
    ):
        return True
    columns = section._sectPr.find(qn("w:cols"))
    try:
        return columns is not None and int(columns.get(qn("w:num"), "1") or 1) > 1
    except ValueError:
        return False


def rebuild_docx(source, *, allow_omissions=False, keep_page_format=False):
    layout, omissions = _analyze_docx(source, keep_page_format)
    if omissions and not allow_omissions:
        raise RebuildUnsupported(omissions)
    result = _new_document(layout)
    output = BytesIO()
    result.save(output)
    output.seek(0)
    return output
