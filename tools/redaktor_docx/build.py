"""Buduje jeden plik `redaktor_docx.py` z programów CMS (Odkurzacz, konwersja, powtórzenia, przebudowa).

Uruchomienie: python tools/redaktor_docx/build.py [plik_wynikowy]

Plik wynikowy to scalone moduły core/services (bez importów między nimi), czcionki PDF zapisane
w treści pliku i interfejs okienkowy z tools/redaktor_docx/gui.py. Kod programów nie jest tu
kopiowany ręcznie – po każdej zmianie w core/services wystarczy zbudować plik ponownie.
"""
import ast
import base64
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / 'core' / 'services'
# Kolejność: moduł może korzystać tylko z wcześniejszych (nazwy są wspólne w jednym pliku).
MODULES = ('document_xml', 'document_errors', 'document_progress', 'document_stories', 'document_styles',
           'document_whitespace', 'document_formatting', 'document_images', 'document_rebuild', 'document_html',
           'document_pdf', 'odkurzacz', 'document_repetitions', 'document_conversion_worker', 'document_preparation')
# Nazwy, które w dwóch modułach znaczą co innego – w scalonym pliku dostają osobne nazwy.
RENAMES = {'document_repetitions': {'WORD_RE': 'REPETITION_WORD_RE'}}
# Funkcje serwera, których program lokalny nie używa (nie są kopiowane).
DROPPED = {'document_conversion_worker': {'main'}}
FONT_FILES = ('NimbusRoman-Regular.ttf', 'NimbusRoman-Bold.ttf', 'NimbusRoman-Italic.ttf', 'NimbusRoman-BoldItalic.ttf')


def _bundled_import(node):
    return isinstance(node, ast.ImportFrom) and (node.level == 1 or node.module in MODULES) \
        and (node.module or '').lstrip('.') in MODULES


def _import_only(statements):
    return bool(statements) and all(_bundled_import(item) for item in statements)


def _removals(tree):
    """Zakresy wierszy do usunięcia oraz aliasy (import ... as ...) do odtworzenia."""
    ranges, aliases = [], []

    def record(node):
        for alias in node.names:
            if alias.asname and alias.asname != alias.name:
                aliases.append((alias.asname, alias.name))

    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == '__package__' \
                and _import_only(node.body) and _import_only(node.orelse):
            ranges.append((node.lineno, node.end_lineno))
            for item in node.body:
                record(item)
        elif isinstance(node, ast.Try) and _import_only(node.body) and all(_import_only(h.body) for h in node.handlers):
            ranges.append((node.lineno, node.end_lineno))
            for item in node.body:
                record(item)
    covered = set()
    for start, end in ranges:
        covered.update(range(start, end + 1))
    for node in ast.walk(tree):
        if _bundled_import(node) and node.lineno not in covered:
            ranges.append((node.lineno, node.end_lineno))
            record(node)
    return ranges, aliases


def transform(name):
    source = (SERVICES / f'{name}.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    ranges, aliases = _removals(tree)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in DROPPED.get(name, ()):
            ranges.append((node.lineno, node.end_lineno))
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            ranges.append((node.lineno, node.end_lineno))
    lines = source.splitlines()
    for start, end in ranges:
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        lines[start - 1] = ' ' * indent + 'pass  # (import scalony w jeden plik)'
        for number in range(start, end):
            lines[number] = ''
    text = '\n'.join(lines)
    for old, new in RENAMES.get(name, {}).items():
        text = re.sub(rf'\b{re.escape(old)}\b', new, text)
    if name == 'document_pdf':
        before = text
        text = text.replace("font_dir = Path(__file__).with_name('fonts')", 'font_dir = _bundled_font_dir()')
        assert text != before, 'document_pdf: brak wiersza z katalogiem czcionek'
    alias_lines = ''.join(f'{new} = {old}\n' for new, old in dict(aliases).items()
                          if new not in RENAMES.get(name, {}))
    return f'\n\n# ===== {name}.py =====\n{text}\n{alias_lines}'


def fonts_section():
    rows = []
    for file_name in FONT_FILES:
        payload = base64.b64encode(zlib.compress((SERVICES / 'fonts' / file_name).read_bytes(), 9)).decode()
        rows.append(f"    {file_name!r}: (\n" + ''.join(
            f'        {payload[index:index + 100]!r}\n' for index in range(0, len(payload), 100)) + '    ),')
    licence = (SERVICES / 'fonts' / 'LICENSE.txt').read_text(encoding='utf-8', errors='replace')
    return ('\n\n# ===== czcionki PDF (Nimbus Roman, licencja poniżej) =====\n'
            'BUNDLED_FONTS = {\n' + '\n'.join(rows) + '\n}\n'
            'FONT_LICENSE = ' + repr(licence) + '\n')


def build(target):
    header = ('#!/usr/bin/env python3\n'
              '"""Redaktor DOCX – Odkurzacz, konwersja DOCX → PDF/EPUB, kolorowanie powtórzeń i przebudowa DOCX.\n\n'
              'Jeden plik zbudowany z programów Fantazmaty CMS (tools/redaktor_docx/build.py); nie edytuj go ręcznie.\n'
              'Uruchomienie: python redaktor_docx.py – otwiera okno programu.\n\n'
              'Wymagania: Python 3.12+, pip install python-docx lxml mammoth EbookLib fpdf2 Pillow rapidfuzz spacy\n'
              'oraz model: pip install pl_core_news_sm==3.8.0 --find-links '
              'https://github.com/explosion/spacy-models/releases/download/pl_core_news_sm-3.8.0/'
              'pl_core_news_sm-3.8.0-py3-none-any.whl\n"""\n'
              'from io import BytesIO  # noqa: F401 – wspólne dla scalonych modułów\n')
    gui = (Path(__file__).with_name('gui.py').read_text(encoding='utf-8'))
    body = header + ''.join(transform(name) for name in MODULES) + fonts_section() + '\n\n' + gui
    compile(body, str(target), 'exec')
    Path(target).write_text(body, encoding='utf-8')
    return target


if __name__ == '__main__':
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'redaktor_docx.py'
    print(build(output))
