"""Wytyczne stron zewnętrznych (audiobooki, ilustracje): jeden parser → bloki → HTML na stronie i PDF.

Treść to lekki, bezpieczny podzbiór Markdownu (ściąga w adminie – ``SYNTAX_HELP``). Wszystko jest
escapowane; linki prowadzą tylko do http, https i mailto. Zwykły tekst bez znaczników też wygląda
czytelnie: pusta linia dzieli akapity, Enter łamie wiersz, linie zaczynające się od „-”, „*”, „•”, „–”
(i podobnych punktorów) albo od „1.”/„1)” tworzą listy. Gdy w tekście nie ma żadnego nagłówka „#”,
nagłówkami zostają też krótkie samodzielne linie pisane WERSALIKAMI (co najmniej 4 litery, do 80 znaków)
oraz krótkie linie zakończone dwukropkiem (do 60 znaków, bez kropki, pytajnika ani wykrzyknika w środku
i bez linków) – te drugie jako podtytuły.
"""
import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import groupby

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import models
from django.http import Http404, HttpResponse
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe

MAX_LENGTH = 40000
HELP_TEXT = 'Tekst publiczny: rozwijany na stronie zewnętrznej i do pobrania jako PDF.'
# Zmiana wyglądu PDF wymaga nowej wersji – inaczej pamięć podręczna oddawałaby stary plik.
PDF_VERSION = 1
PDF_CACHE_SECONDS = 7 * 24 * 3600

SYNTAX_HELP = mark_safe(
    'Składnia (wszystko poza nią, także HTML, jest pokazywane jako zwykły tekst):<ul>'
    '<li>pusta linia – nowy akapit, Enter – nowa linia w akapicie;</li>'
    '<li><code># Nagłówek</code>, <code>## Podtytuł</code>;</li>'
    '<li><code>- punkt</code> (także <code>*</code>, <code>•</code>, <code>–</code>), '
    '<code>1. punkt</code> albo <code>1) punkt</code>;</li>'
    '<li><code>**pogrubienie**</code>, <code>_kursywa_</code>, <code>---</code> – linia oddzielająca;</li>'
    '<li><code>[tekst linku](https://…)</code>, <code>[napisz](mailto:adres@…)</code> – adresy e-mail '
    'i <code>https://…</code> same stają się linkami;</li>'
    '<li>bez nagłówków <code>#</code> w tekście krótka linia pisana WERSALIKAMI albo zakończona dwukropkiem '
    'staje się nagłówkiem; pierwszy nagłówek równy tytułowi panelu nie jest powtarzany.</li></ul>'
)


def guidelines_field():
    """Pole wytycznych w ustawieniach stron zewnętrznych – te same limity dla audiobooków i ilustracji."""
    return models.TextField('wytyczne', blank=True, default='', max_length=MAX_LENGTH, help_text=HELP_TEXT)


@dataclass(frozen=True)
class Run:
    """Fragment wiersza o jednym formatowaniu; ``href`` – bezpieczny adres linku albo ''."""
    text: str
    bold: bool = False
    italic: bool = False
    href: str = ''


@dataclass(frozen=True)
class Block:
    """Blok treści: ``heading`` (``level`` 1–2), ``paragraph`` (``lines``), ``list`` (``items`` – punkty
    z wierszami, ``ordered``, ``start``) albo ``rule``. Wiersz to krotka ``Run``."""
    kind: str
    lines: tuple = ()
    items: tuple = ()
    level: int = 1
    ordered: bool = False
    start: int = 1

    @property
    def text(self):
        return ' '.join(''.join(run.text for run in line) for line in self.lines)


HEADING = re.compile(r'(#{1,6})\s+(.+?)(?:\s+#+)?\s*')
BULLET = re.compile(r'[-*•–·◦▪●]\s+(.*)')
NUMBER = re.compile(r'(\d{1,3})[.)]\s+(.*)')
RULE = re.compile(r'([-*_=])(?:\s*\1){2,}')
SENTENCE_END = re.compile(r'[.!?…]\s')
# Pogrubienie, kursywa i etykieta linku mają limit długości: tekst z wieloma niedomkniętymi „**” czy „[”
# nie może kazać wyrażeniu przeszukiwać całego wiersza od każdego z nich (czas kwadratowy).
SPAN = 400
INLINE = re.compile(
    rf'\[(?P<label>[^\]\n]{{1,{SPAN}}})\]\((?P<target>(?:[^\s()]|\([^\s()]*\)){{1,2000}})\)'
    rf'|\*\*(?P<bold>\S(?:.{{0,{SPAN}}}?\S)?)\*\*'
    rf'|(?<!\w)_(?P<italic>[^\s_](?:.{{0,{SPAN}}}?[^\s_])?)_(?!\w)'
    r'|(?<![\w@./-])(?P<url>(?i:https?://|www\.)[^\s<>"]+)'
    r'|(?<![\w.+-])(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,})(?![\w-])'
)
URL_TRAILING = '.,;:!?\'"”’»…]}'
is_web_url = URLValidator(schemes=['http', 'https'])
is_email = EmailValidator()


def _valid(validator, value):
    try:
        validator(value)
    except ValidationError:
        return False
    return True


def safe_href(target):
    """Adres linku tylko dla http(s) i mailto (także sam adres e-mail albo „www.…”); inaczej ''."""
    target = target.strip()
    lowered = target.lower()
    if lowered.startswith('www.'):
        target, lowered = 'https://' + target, 'https://' + lowered
    if lowered.startswith(('http://', 'https://')):
        return target if _valid(is_web_url, target) else ''
    if lowered.startswith('mailto:'):
        address = target[len('mailto:'):].split('?', 1)[0]
        return target if _valid(is_email, address) else ''
    return 'mailto:' + target if '@' in target and _valid(is_email, target) else ''


def _trim_url(url):
    """Kropka, przecinek czy nawias zamykający zdanie nie należą do adresu (chyba że nawias ma parę)."""
    end = len(url)
    while end and (url[end - 1] in URL_TRAILING
                   or url[end - 1] == ')' and url.count('(', 0, end) < url.count(')', 0, end)):
        end -= 1
    return url[:end], url[end:]


def parse_inline(text, bold=False, italic=False, href=''):
    """Wiersz → krotka ``Run``. W etykiecie linku (``href``) adresy nie tworzą zagnieżdżonych linków."""
    runs, position = [], 0

    def plain(value):
        if value:
            runs.append(Run(value, bold, italic, href))

    for match in INLINE.finditer(text):
        plain(text[position:match.start()])
        position = match.end()
        if match['label']:
            # Niedozwolony adres (np. javascript:) zostawia samą etykietę, bez linku.
            runs.extend(parse_inline(match['label'], bold, italic, href or safe_href(match['target'])))
        elif match['bold']:
            runs.extend(parse_inline(match['bold'], True, italic, href))
        elif match['italic']:
            runs.extend(parse_inline(match['italic'], bold, True, href))
        else:
            value, trailing = _trim_url(match['url']) if match['url'] else (match['email'], '')
            runs.append(Run(value, bold, italic, href or safe_href(value)))
            plain(trailing)
    plain(text[position:])
    merged = []
    for run in runs:
        if merged and (merged[-1].bold, merged[-1].italic, merged[-1].href) == (run.bold, run.italic, run.href):
            merged[-1] = Run(merged[-1].text + run.text, run.bold, run.italic, run.href)
        else:
            merged.append(run)
    return tuple(merged)


def _plain_heading(line):
    """Poziom nagłówka zwykłego tekstu (1 – WERSALIKI, 2 – krótka linia z dwukropkiem) albo None."""
    if len(line) > 80 or any(INLINE.finditer(line)):
        return None
    letters = [character for character in line if character.isalpha()]
    if len(letters) >= 4 and all(character.isupper() for character in letters):
        return 1
    if line.endswith(':') and len(line) <= 60 and letters and not SENTENCE_END.search(line):
        return 2
    return None


def parse(text):
    """Tekst wytycznych → krotka ``Block``."""
    lines = (text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    marked = any(HEADING.fullmatch(line.strip()) for line in lines)
    blocks, paragraph, items = [], [], []
    list_kind = None  # (numerowana?, początek)

    def flush():
        nonlocal list_kind
        if paragraph:
            blocks.append(Block('paragraph', lines=tuple(parse_inline(line) for line in paragraph)))
            paragraph.clear()
        if items:
            blocks.append(Block('list', items=tuple(tuple(parse_inline(line) for line in item) for item in items),
                                ordered=list_kind[0], start=list_kind[1]))
            items.clear()
            list_kind = None

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            continue
        if RULE.fullmatch(line):
            flush()
            blocks.append(Block('rule'))
            continue
        heading = HEADING.fullmatch(line)
        if heading:
            flush()
            blocks.append(Block('heading', lines=(parse_inline(heading[2]),), level=min(len(heading[1]), 2)))
            continue
        bullet, number = BULLET.fullmatch(line), NUMBER.fullmatch(line)
        if bullet or number:
            kind = (False, 1) if bullet else (True, int(number[1]))
            if paragraph or (list_kind and list_kind[0] != kind[0]):
                flush()
            list_kind = list_kind or kind
            items.append([(bullet or number)[1 if bullet else 2]])
            continue
        if items and raw[:1].isspace():
            items[-1].append(line)  # wcięta linia ciągnie dalej poprzedni punkt
            continue
        if items:
            flush()
        level = None if marked else _plain_heading(line)
        if level:
            flush()
            blocks.append(Block('heading', lines=(parse_inline(line),), level=level))
            continue
        paragraph.append(line)
    flush()
    return tuple(blocks)


def _runs_html(runs):
    parts = []
    for href, group in groupby(runs, key=lambda run: run.href):
        inner = ''
        for run in group:
            value = escape(run.text)
            if run.italic:
                value = f'<em>{value}</em>'
            if run.bold:
                value = f'<strong>{value}</strong>'
            inner += value
        if href:
            external = '' if href.startswith('mailto:') else ' target="_blank" rel="noopener noreferrer"'
            inner = f'<a href="{escape(href)}"{external}>{inner}</a>'
        parts.append(inner)
    return ''.join(parts)


def _lines_html(lines):
    return '<br>'.join(_runs_html(line) for line in lines)


def to_html(blocks):
    """Bloki → bezpieczny HTML (nagłówki h3/h4, bo panel ma tytuł h2)."""
    parts = []
    for block in blocks:
        if block.kind == 'heading':
            tag = 'h3' if block.level == 1 else 'h4'
            parts.append(f'<{tag}>{_lines_html(block.lines)}</{tag}>')
        elif block.kind == 'paragraph':
            parts.append(f'<p>{_lines_html(block.lines)}</p>')
        elif block.kind == 'list':
            tag = 'ol' if block.ordered else 'ul'
            start = f' start="{block.start}"' if block.ordered and block.start != 1 else ''
            items = ''.join(f'<li>{_lines_html(item)}</li>' for item in block.items)
            parts.append(f'<{tag}{start}>{items}</{tag}>')
        else:
            parts.append('<hr>')
    # Każdy tekst i adres przeszedł przez escape().
    return mark_safe('\n'.join(parts))


def external_page_headers(response):
    """Strony zewnętrzne i ich pliki: bez indeksowania i bez adresu strony w nagłówku Referer."""
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@lru_cache(maxsize=8)
def rendered(title, text):
    """(bloki, HTML) wytycznych – treść jest parsowana raz w procesie, nie przy każdym wyświetleniu strony.

    Wklejony dokument często zaczyna się tytułem – panel i PDF mają go już w nagłówku, więc pierwszy
    nagłówek równy tytułowi nie jest powtarzany."""
    blocks = parse(text)
    if blocks and blocks[0].kind == 'heading' and blocks[0].text.strip(' :').casefold() == title.casefold():
        blocks = blocks[1:]
    return blocks, to_html(blocks)


@dataclass(frozen=True)
class PublicGuidelines:
    """Wytyczne jednej strony zewnętrznej: tytuł panelu i PDF, nazwa pliku i trasa pobierania."""
    title: str
    filename: str
    pdf_route: str

    def context(self, text):
        """Kontekst szablonu core/includes/public_guidelines.html (pusty ``html`` – wytyczne do uzupełnienia)."""
        return {'title': self.title, 'html': rendered(self.title, text)[1], 'pdf_url': reverse(self.pdf_route)}

    def pdf_response(self, text):
        """PDF do pobrania z pamięci podręcznej (klucz – skrót treści); 404, gdy wytycznych brak."""
        blocks = rendered(self.title, text)[0]
        if not blocks:
            raise Http404('Wytyczne nie zostały jeszcze uzupełnione.')
        digest = hashlib.sha256(f'{PDF_VERSION}\0{self.title}\0{text}'.encode()).hexdigest()
        key = f'public-guidelines-pdf:{digest}'
        content = cache.get(key)
        if content is None:
            from core.services.guidelines_pdf import render_guidelines_pdf

            content = render_guidelines_pdf(blocks, self.title)
            cache.set(key, content, PDF_CACHE_SECONDS)
        response = HttpResponse(content, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{self.filename}"'
        return external_page_headers(response)
