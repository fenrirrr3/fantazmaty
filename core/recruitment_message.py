"""Read the generated recruitment form; retain the original message unchanged."""
import re
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from core.recruitment_roles import ROLE_CHOICES


def name_in_subject(subject):
    value = re.sub(r'^\s*(?:(?:Nowe\s+)?zgłoszenie\s+(?:do\s+Fantazmatów|rekrutacyjne)|Rekrutacja)\s*', '', subject, flags=re.I)
    value = re.sub(r'\b(?:wybrane role|role|imię i nazwisko)\s*:', '', value, flags=re.I)
    for _, label in sorted(ROLE_CHOICES, key=lambda row: -len(row[1])):
        value = re.sub(r'(?<!\w)' + re.escape(label) + r'(?!\w)', '', value, flags=re.I)
    value = re.sub(r'\s+[–—-]\s+|[|:;,()[\]]', ' ', value)
    value = ' '.join(value.strip(' –—-').split())
    # Do not present arbitrary subjects or unresolved form tags as identities.
    words = value.split()
    if any(word.casefold().startswith(('zgłoszen', 'rekrutac')) for word in words):
        return ''
    if 2 <= len(words) <= 8 and all(all(c.isalpha() or c in "-’'." for c in word) for word in words):
        return value
    return ''


def roles_in_subject(subject):
    remaining, matched = subject.casefold(), set()
    for key, label in sorted(ROLE_CHOICES, key=lambda choice: -len(choice[1])):
        if label.casefold() in remaining:
            matched.add(key)
            remaining = remaining.replace(label.casefold(), ' ')
    return [key for key, _ in ROLE_CHOICES if key in matched]


def form_fields(body):
    # Only parse the labelled header before WIADOMOŚĆ, never a quoted form in prose.
    header = re.split(r'^\s*WIADOMOŚĆ\s*$', body, maxsplit=1, flags=re.M | re.I)
    if len(header) != 2 or not re.match(r'\s*Nowe zgłoszenie do Fantazmatów\s*(?:\r?\n|$)', header[0], re.I):
        return {}
    def field(label):
        matches = re.findall(r'^' + re.escape(label) + r':[ \t]*(.*)$', header[0], re.M | re.I)
        value = matches[0].strip() if len(matches) == 1 else ''
        return '' if re.fullmatch(r'\[[^\]]+\]', value) else value
    name = ' '.join(field('Imię i nazwisko').split())
    if len(name) > 300:
        name = ''  # Original text still available; never silently truncate identity.
    email = field('E-mail').lower()
    try:
        validate_email(email)
        if len(email) > 254:
            email = ''
    except ValidationError:
        email = ''
    selected = {value.strip().casefold() for value in re.split(r'[,;|]', field('Wybrane role'))}
    roles = [key for key, label in ROLE_CHOICES if label.casefold() in selected]
    return {'name': name, 'email': email, 'roles': roles}
