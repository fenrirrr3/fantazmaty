"""Oczyszczanie notki działu na stronie WWW: zostają tylko <a href="http(s)://…">, <strong> i <em>.

Bez zewnętrznych zależności (bleach nie ma w projekcie): tekst jest escapowany, dozwolone znaczniki
odtwarzane bez innych atrybutów, pozostałe znaczniki usuwane (ich treść zostaje).
"""
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = ('a', 'strong', 'em')


class _NoteCleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.open_tags = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            return
        if tag == 'a':
            href = (dict(attrs).get('href') or '').strip()
            if not href.lower().startswith(('https://', 'http://')):
                return
            self.parts.append(f'<a href="{escape(href)}">')
        else:
            self.parts.append(f'<{tag}>')
        self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if tag in self.open_tags:
            while self.open_tags:
                closed = self.open_tags.pop()
                self.parts.append(f'</{closed}>')
                if closed == tag:
                    break

    def handle_data(self, data):
        self.parts.append(escape(data, quote=False))

    def close(self):
        super().close()
        while self.open_tags:
            self.parts.append(f'</{self.open_tags.pop()}>')


def clean_note(value):
    """HTML notki ograniczony do dozwolonych znaczników; pusta wartość daje pusty tekst."""
    cleaner = _NoteCleaner()
    cleaner.feed((value or '').strip())
    cleaner.close()
    return ''.join(cleaner.parts).strip()
