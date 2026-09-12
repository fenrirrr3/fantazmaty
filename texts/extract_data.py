"""Czytanie zestawień Ekstraktów bez dodatkowych zależności."""
import html
import re
from datetime import date
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

HEADERS = ['Imię i nazwisko', 'Adres e-mail', 'Numer telefonu', 'Tytuł', 'Data nadesłania', 'Nabór', 'Przyjęte', 'Odrzucone']


def normalize_key(value):
    return ' '.join(value.split()).casefold()


def split_list(value):
    result, seen = [], set()
    for part in re.split(r'[;\r\n]+', value or ''):
        part = part.strip()
        key = normalize_key(part)
        if key and key not in seen:
            result.append(part)
            seen.add(key)
    return result


def parse_dates(value):
    values = split_list(value)
    if not values:
        raise ValueError('Wpisz co najmniej jedną datę nadesłania.')
    for value in values:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Data musi mieć format RRRR-MM-DD.')
        date.fromisoformat(value)
    return sorted(set(values))


def read_markdown(content):
    rows, header_seen = [], False
    for number, line in enumerate(content.lstrip('\ufeff').splitlines(), 1):
        if not line.strip():
            continue
        line = line.strip()
        if not line.startswith('|') or not line.endswith('|'):
            raise ValueError(f'Wiersz {number}: oczekiwano tabeli Markdown.')
        cells = [html.unescape(c.strip().replace(r'\|', '|')) for c in re.split(r'(?<!\\)\|', line)[1:-1]]
        if len(cells) != 8:
            raise ValueError(f'Wiersz {number}: oczekiwano 8 kolumn, otrzymano {len(cells)}.')
        if not header_seen:
            if [c.strip('* ') for c in cells] != HEADERS:
                raise ValueError('Nieprawidłowe nagłówki tabeli.')
            header_seen = True
            continue
        if all(re.fullmatch(r':?-+:?', c.replace(' ', '')) for c in cells):
            continue
        cells = [re.sub(r'\\([\\`*_{}\[\]()#+.!|:-])', r'\1', cell) for cell in cells]
        name, address, phone, titles, dates, recruitment, accepted, rejected = cells
        match = re.fullmatch(r'\[([^\]]+)\]\(mailto\\?:([^\)]+)\)', address)
        if match:
            if normalize_key(match[1]) != normalize_key(match[2]):
                raise ValueError(f'Wiersz {number}: etykieta e-maila różni się od odnośnika.')
            address = match[1]
        address = address.strip().lower()
        try:
            validate_email(address)
            dates = parse_dates(dates)
        except (ValidationError, ValueError) as error:
            raise ValueError(f'Wiersz {number}: nieprawidłowy adres lub data.') from error
        if not name or not recruitment or not split_list(titles):
            raise ValueError(f'Wiersz {number}: brak nazwiska, naboru albo tytułów.')
        rows.append(dict(line=number, full_name=name, email=address, phone_number=phone,
            title='\n'.join(split_list(titles)), submission_dates='\n'.join(dates),
            recruitment=' '.join(recruitment.split()), accepted_titles='\n'.join(split_list(accepted)),
            rejected_titles='\n'.join(split_list(rejected))))
    if not rows:
        raise ValueError('Plik nie zawiera zgłoszeń.')
    return rows


def author_names(full_name):
    # Pełna oryginalna nazwa (wraz z pseudonimem) pozostaje w Extract.full_name.
    base = re.split(r'\s*/\s*|\s*\(', full_name, maxsplit=1)[0].strip()
    parts = base.split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError('Nie można rozdzielić imienia i nazwiska; utwórz autora ręcznie.')
    return parts
