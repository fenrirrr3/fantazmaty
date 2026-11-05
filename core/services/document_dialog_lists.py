"""Dialogi zapisane jako lista punktowana: zamiana na zwykłe akapity z myślnikiem dialogowym.

Word zamienia wpisane na początku akapitu „- ” w punkt listy (Autoformatowanie podczas pisania), więc kwestie
dialogowe trafiają do listy z punktorem „-”. Konwerter pokazuje je wtedy jako punkty listy (•), a Odkurzacz nie
widzi myślnika dialogowego. Krok działa tylko wtedy, gdy dokument wygląda na taki przypadek (DialogDetection):
punkty listy z punktorem-kreską obok kwestii zapisanych prawidłowo albo w kilku miejscach tekstu. Wtedy punkt listy dostaje „– ” na początku, formatowanie zwykłego akapitu tekstu i traci numerację.

Tekst wklejony z PDF-u bywa podzielony na końcu wiersza: kwestia kończy się w pół zdania, a reszta stoi w osobnym
akapicie bez wcięcia. Taki akapit tuż po zamienionej kwestii (bez wcięcia pierwszego wiersza i tabulatora, gdy
kwestia nie kończy się znakiem końca zdania) jest dołączany do kwestii.

Śledzone zmiany zostają: akapity ze zmianą znaku akapitu, z w:sectPr albo w treści usuniętej nie są łączone,
a tekst usunięty (w:del, w:moveFrom) nie liczy się do wykrywania. Działa na document.xml (treść główna z tabelami).
"""
from bisect import bisect_left
from copy import deepcopy
from io import BytesIO
import re
from zipfile import ZipFile

from lxml import etree

if __package__:
    from .document_formatting import _child, _schema_order
    from .document_styles import ParagraphOutline, _paragraph_style_id
    from .document_whitespace import DELETED, SPACE
    from .document_xml import parse_part, serialize_part, w
else:
    from document_formatting import _child, _schema_order
    from document_styles import ParagraphOutline, _paragraph_style_id
    from document_whitespace import DELETED, SPACE
    from document_xml import parse_part, serialize_part, w

DOCUMENT = 'word/document.xml'
# Punktory-kreski: łącznik, półpauza, pauza, minus i ich odmiany; w foncie Symbol łącznik to znak U+F02D.
DASH_BULLETS = frozenset('-‐‑‒–—―−')
DIALOG_DASH = '–'
# Koniec zdania (także przed zamykającym cudzysłowem lub nawiasem): kwestia się nie urywa.
FINISHED = re.compile(r'[.!?…][\s"”»’\')\]]*\Z')
# Kwestia zapisana prawidłowo: zwykły akapit zaczynający się kreską (z odstępem lub bez) przed tekstem.
WRITTEN = re.compile(r'[ \t]*[-–—][ \t\u00a0]*\w')
# Wtrącenie narratora: „Dziwka – odezwała się…”, „Nie podchodź tam! - matka szarpnęła…”.
NARRATOR = re.compile(r'\s[-–—]\s*[a-ząćęłńóśźż]')
# Formatowanie zwykłego akapitu tekstu kopiowane do zamienionej kwestii.
LAYOUT = ('pStyle', 'spacing', 'ind', 'contextualSpacing')
# Wyróżnienia pierwszego przebiegu, których myślnik dialogowy nie przejmuje.
EMPHASIS = ('rStyle', 'b', 'bCs', 'i', 'iCs', 'u', 'strike', 'dstrike', 'highlight', 'vertAlign', 'caps', 'smallCaps')


def paragraph_text(paragraph):
    """Widoczny tekst akapitu (bez tekstu usuniętego śledzoną zmianą); tabulator to „\\t”."""
    parts = []
    for node in paragraph.iter(w('t'), w('tab'), w('br'), w('noBreakHyphen')):
        if any(parent.tag in DELETED for parent in node.iterancestors()):
            continue
        if node.tag == w('t'):
            parts.append(node.text or '')
        elif node.tag == w('tab'):
            parts.append('\t')
        elif node.tag == w('noBreakHyphen'):
            parts.append('-')
        else:
            parts.append('\n')
    return ''.join(parts)


class Numbering:
    """Punktory list z numbering.xml: (numId, poziom) → znak punktora albo None (numeracja liczbami)."""

    def __init__(self, root):
        self.abstract, self.nums = {}, {}
        if root is None:
            return
        for node in root.iter(w('abstractNum')):
            self.abstract[node.get(w('abstractNumId'))] = node
        for node in root.iter(w('num')):
            target = node.find(w('abstractNumId'))
            if target is not None:
                self.nums[node.get(w('numId'))] = (target.get(w('val')), node)

    def _level(self, num_id, level):
        abstract_id, num = self.nums.get(num_id, (None, None))
        if num is None:
            return None
        for override in num.iter(w('lvlOverride')):
            if override.get(w('ilvl')) == level and override.find(w('lvl')) is not None:
                return override.find(w('lvl'))
        abstract = self.abstract.get(abstract_id)
        if abstract is None:
            return None
        link = abstract.find(w('numStyleLink'))
        if link is not None:  # lista ze stylu numeracji – rzadka w tekstach; bez rozwijania
            return None
        for node in abstract.iter(w('lvl')):
            if node.get(w('ilvl'), '0') == level:
                return node
        return None

    def dash_bullet(self, num_id, level):
        """Czy poziom listy ma punktor-kreskę (np. „-” z Autoformatowania Worda)."""
        node = self._level(num_id, level or '0')
        if node is None:
            return False
        fmt = node.find(w('numFmt'))
        text = node.find(w('lvlText'))
        if fmt is None or fmt.get(w('val')) != 'bullet' or text is None:
            return False
        return (text.get(w('val')) or '').strip() in DASH_BULLETS and len((text.get(w('val')) or '').strip()) == 1


def _numbering(paragraph, outline):
    """(numId, ilvl) akapitu z numPr akapitu albo stylu; None bez listy (albo z numId 0)."""
    properties = paragraph.find(w('pPr'))
    node = properties.find(w('numPr')) if properties is not None else None
    num_id = node.find(w('numId')) if node is not None else None
    if num_id is None:
        num_id = outline._style_numbering(_paragraph_style_id(properties))
        node = num_id.getparent() if num_id is not None else None
    if num_id is None or num_id.get(w('val')) in (None, '0'):
        return None
    level = node.find(w('ilvl')) if node is not None else None
    return num_id.get(w('val')), level.get(w('val'), '0') if level is not None else '0'


def _indent(paragraph, outline):
    """(lewy margines, wcięcie pierwszego wiersza) akapitu w twipach – z akapitu albo łańcucha stylów."""
    properties = paragraph.find(w('pPr'))
    owners = [properties] if properties is not None else []
    owners += [style.find(w('pPr')) for _, style in outline._chain(_paragraph_style_id(properties)) if style is not None]
    left = first = None
    for owner in owners:
        node = owner.find(w('ind')) if owner is not None else None
        if node is None:
            continue
        if left is None and (node.get(w('left')) or node.get(w('start'))) is not None:
            left = node.get(w('left')) or node.get(w('start'))
        if first is None:
            if node.get(w('hanging')) is not None:
                first = '-' + node.get(w('hanging'))
            elif node.get(w('firstLine')) is not None:
                first = node.get(w('firstLine'))
    return _twips(left), _twips(first)


def _twips(value):
    try:
        return int(float(value)) if value is not None else 0
    except ValueError:
        return 0


class DialogDetection:
    """Kwestie dialogowe zapisane jako lista: punkty z punktorem-kreską w treści dokumentu.

    Dokument jest takim przypadkiem, gdy ma takie punkty i choć jedną kwestię zapisaną prawidłowo (zwykły akapit
    zaczynający się kreską – część dialogów bywa zapisana dobrze, część jako lista) albo co najmniej dwa takie
    punkty w co najmniej dwóch miejscach tekstu (przedzielonych zwykłymi akapitami), z choć jednym wtrąceniem
    narratora albo w co najmniej trzech miejscach. Wtedy zamieniane są wszystkie punkty z punktorem-kreską.
    Zwykła lista z punktorem-kreską (jedna lista w tekście, wyliczenie bez wtrąceń) zostaje listą.
    """

    def __init__(self, paragraphs, outline, numbering):
        self.items = []
        groups, previous, written = 0, False, 0
        for paragraph in paragraphs:
            numbered = _numbering(paragraph, outline)
            plain = outline.heading_level(paragraph) is None
            dash = numbered is not None and plain and numbering.dash_bullet(*numbered)
            if dash:
                self.items.append(paragraph)
                groups += not previous
            elif numbered is None and plain and WRITTEN.match(paragraph_text(paragraph)):
                written += 1
            previous = dash
        narrated = any(NARRATOR.search(paragraph_text(item)) for item in self.items)
        self.found = bool(self.items) and (bool(written) or (
            len(self.items) >= 2 and groups >= 2 and (narrated or groups >= 3)))


class BodyLayouts:
    """Formatowanie zwykłego akapitu tekstu (styl, odstępy, wcięcie) dla zamienionej kwestii: z najbliższego
    wcześniejszego zwykłego akapitu (gdy go nie ma – następnego), tak jak w otoczeniu kwestii.

    Pierwszeństwo mają akapity z wcięciem pierwszego wiersza bez lewego marginesu, potem akapity bez lewego
    marginesu (lewy margines bywa pozostałością listy), na końcu dowolne. Bez zwykłych akapitów – None.
    """

    def __init__(self, paragraphs, outline, skip):
        bodies = []
        for index, paragraph in enumerate(paragraphs):
            if paragraph in skip or not paragraph_text(paragraph).strip() or \
                    outline.heading_level(paragraph) is not None or outline.is_list_item(paragraph):
                continue
            properties = paragraph.find(w('pPr'))
            layout = tuple(properties.find(w(name)) if properties is not None else None for name in LAYOUT)
            left, first = _indent(paragraph, outline)
            bodies.append((index, layout, first > 0 and left == 0, left == 0))
        for rank in (2, 3):
            if any(body[rank] for body in bodies):
                bodies = [body for body in bodies if body[rank]]
                break
        self.positions = [index for index, *_ in bodies]
        self.layouts = [layout for _, layout, *_ in bodies]
        self.order = {paragraph: index for index, paragraph in enumerate(paragraphs)}

    def near(self, paragraph):
        if not self.layouts:
            return None
        found = bisect_left(self.positions, self.order[paragraph])
        return self.layouts[found - 1 if found else 0]


def _apply_layout(paragraph, layout, outline):
    """Akapit bez numeracji, ze stylem, odstępami i wcięciem zwykłego akapitu tekstu."""
    properties = paragraph.find(w('pPr'))
    if properties is None:
        properties = etree.Element(w('pPr'))
        paragraph.insert(0, properties)
    for node in properties.findall(w('numPr')):
        properties.remove(node)
    for name, sample in zip(LAYOUT, layout or (None,) * len(LAYOUT)):
        for node in properties.findall(w(name)):
            properties.remove(node)
        if sample is not None:
            properties.replace(_child(properties, 'w:' + name), deepcopy(sample))
    if outline.is_list_item(paragraph):
        # Numeracja ze stylu akapitu (np. „Lista punktowana”) – wyłączona w akapicie (numId 0).
        numbering = _child(properties, 'w:numPr')
        etree.SubElement(numbering, w('numId')).set(w('val'), '0')
    _schema_order(properties)


def _content_runs(paragraph):
    """Przebiegi z widocznym tekstem w kolejności (bez tekstu usuniętego)."""
    return [run for run in paragraph.iter(w('r'))
            if run.find(w('t')) is not None and not any(parent.tag in DELETED for parent in run.iterancestors())]


def _add_dash(paragraph):
    """„– ” na początku kwestii (bez wyróżnień pierwszego przebiegu); kwestia zaczynająca się kreską zostaje."""
    runs = _content_runs(paragraph)
    if not runs:
        return
    first = runs[0].find(w('t'))
    if (first.text or '').lstrip()[:1] in DASH_BULLETS:
        return
    first.text = (first.text or '').lstrip(' \t')
    first.set(SPACE, 'preserve')
    dash = etree.Element(w('r'))
    properties = runs[0].find(w('rPr'))
    if properties is not None:
        copied = deepcopy(properties)
        for name in EMPHASIS:
            for node in copied.findall(w(name)):
                copied.remove(node)
        if len(copied):
            dash.append(copied)
    text = etree.SubElement(dash, w('t'))
    text.text = DIALOG_DASH + ' '
    text.set(SPACE, 'preserve')
    runs[0].addprevious(dash)


def _mark_changed(paragraph):
    """Znak akapitu wstawiony lub usunięty śledzoną zmianą albo koniec sekcji – akapitów nie łączymy."""
    properties = paragraph.find(w('pPr'))
    if properties is None:
        return False
    mark = properties.find(w('rPr'))
    return properties.find(w('sectPr')) is not None or (
        mark is not None and any(mark.find(w(name)) is not None for name in ('ins', 'del', 'moveFrom', 'moveTo')))


def _continuation(paragraph, following, outline):
    """Czy `following` (następny akapit) to dalszy ciąg urwanej kwestii `paragraph`."""
    if following is None or following.tag != w('p') or _mark_changed(paragraph) or _mark_changed(following):
        return False
    text = paragraph_text(following)
    if not text.strip() or text[:1] in '\t ' or outline.heading_level(following) is not None \
            or outline.is_list_item(following) or FINISHED.search(paragraph_text(paragraph).rstrip()):
        return False
    return _indent(following, outline) == (0, 0)


def _merge(paragraph, following):
    """Treść `following` dopisana do `paragraph` (ze spacją między wierszami); `following` znika."""
    before, after = paragraph_text(paragraph), paragraph_text(following)
    if before[-1:] not in ' \n' and after[:1] != ' ':
        runs = _content_runs(paragraph)
        properties = runs[-1].find(w('rPr')) if runs else None
        space = etree.SubElement(paragraph, w('r'))
        if properties is not None:
            space.append(deepcopy(properties))
        node = etree.SubElement(space, w('t'))
        node.text = ' '
        node.set(SPACE, 'preserve')
    for child in list(following):
        if child.tag != w('pPr'):
            paragraph.append(child)
    following.getparent().remove(following)


def convert_dialog_lists(root, outline, numbering):
    """Zamiana w korzeniu document.xml; zwraca (liczba zamienionych kwestii, liczba dołączonych akapitów)."""
    paragraphs = [paragraph for paragraph in root.iter(w('p'))
                  if not any(parent.tag in DELETED for parent in paragraph.iterancestors())]
    detection = DialogDetection(paragraphs, outline, numbering)
    if not detection.found:
        return 0, 0
    # Najpierw dołączenie urwanych wierszy: dołączone akapity nie są wzorem formatowania.
    joined = set()
    for paragraph in detection.items:
        while _continuation(paragraph, paragraph.getnext(), outline):
            joined.add(paragraph.getnext())
            _merge(paragraph, paragraph.getnext())
    layouts = BodyLayouts(paragraphs, outline, joined | set(detection.items))
    for paragraph in detection.items:
        _apply_layout(paragraph, layouts.near(paragraph), outline)
        _add_dash(paragraph)
    return len(detection.items), len(joined)


def dialog_lists_docx(source, report=None):
    """DOCX z kwestiami dialogowymi zamiast listy (gdy wykryto taki przypadek; inaczej plik bez zmian). `report`
    (słownik) dostaje 'dialogs' i 'merged' – liczby zamienionych kwestii i dołączonych urwanych wierszy."""
    payload = source.read()
    dialogs = merged = 0
    with ZipFile(BytesIO(payload)) as archive:
        if len(archive.infolist()) > 2000 or sum(i.file_size for i in archive.infolist()) > 50*1024*1024:
            raise ValueError('Dokument przekracza limit rozpakowanej zawartości.')
        names = set(archive.namelist())
        numbering = Numbering(parse_part(archive.read('word/numbering.xml')) if 'word/numbering.xml' in names else None)
        root = None
        if numbering.nums and DOCUMENT in names:
            outline = ParagraphOutline(parse_part(archive.read('word/styles.xml')) if 'word/styles.xml' in names else None)
            root = parse_part(archive.read(DOCUMENT))
            dialogs, merged = convert_dialog_lists(root, outline, numbering)
        if dialogs:
            output = BytesIO()
            with ZipFile(output, 'w') as target:
                for entry in archive.infolist():
                    target.writestr(entry, serialize_part(root) if entry.filename == DOCUMENT else archive.read(entry))
            payload = output.getvalue()
    if report is not None:
        report.update(dialogs=dialogs, merged=merged)
    return BytesIO(payload)
