"""Polska odmiana rzeczownika po liczebniku – bez Django (formularze programów, Strona WWW, komunikaty admina).

Kopia tej reguły zostaje w ``core/services/document_images.py`` – moduł scala ``tools/redaktor_docx/build.py``.
"""


def plural(count, one, few, many):
    """Polska forma liczebnika: 1 plik, 2–4 (22–24…) pliki, 5–21 (25…) plików."""
    last, two = count % 10, count % 100
    if count == 1:
        return one
    return few if 2 <= last <= 4 and not 12 <= two <= 14 else many


def count_text(count, forms):
    """Liczba z rzeczownikiem w odpowiedniej formie, np. ``count_text(3, ('plik', 'pliki', 'plików'))`` – „3 pliki”."""
    return f'{count} {plural(count, *forms)}'
