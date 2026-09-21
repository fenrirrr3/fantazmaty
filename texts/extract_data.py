"""Czytanie zestawień Ekstraktów bez dodatkowych zależności."""
import re
from datetime import date



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




