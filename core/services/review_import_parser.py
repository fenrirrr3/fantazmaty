"""Parse pasted review rows; form-level duplicate/blacklist checks stay in the form."""
import re
import csv
from io import StringIO
from core.services.newsletters import parse_consents
from django.core.exceptions import ValidationError
from texts.models import Review
from texts.services import normalize_email, normalize_whitespace
from core.normalization import REVIEW_FIELDS, upper

MAX_IMPORT_RECORDS = 500
MAX_IMPORT_CHARACTERS = 1_000_000
MAX_IMPORT_ERRORS = 20

# Named CF7 fields are converted to CSV only inside the import pipeline. Keeping
# first name, surname and pseudonym separate avoids guessing where a name splits.
NAMED_FIELDS = {
    'imię': 'author_first_name',
    'nazwisko': 'author_last_name',
    'pseudonim': 'author_pseudonym',
    'tytuł opowiadania': 'title',
    'gatunek': 'genre',
    'ostrzeżenia o treści': 'content_warnings',
    'liczba znaków ze spacjami': 'length',
    'adres e-mail': 'email',
    'numer telefonu': 'phone_number',
    'newsletter': 'choices',
    'wiadomość do redakcji': 'author_message',
}
NAMED_REQUIRED_FIELDS = ('author_first_name', 'author_last_name', 'title', 'genre', 'length', 'email')
AUTHOR_MESSAGE_END = '--- KONIEC WIADOMOŚCI AUTORA ---'


def named_submission(value):
    """Return one labelled CF7 submission, or None for legacy semicolon input.

    The author message is last: its lines, colons and semicolons are content,
    never more metadata. Missing or repeated metadata fails explicitly.
    """
    lines = value.lstrip('\ufeff').splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or lines[first].partition(':')[0].strip().casefold() not in NAMED_FIELDS:
        return None
    data = {}
    for index in range(first, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        label, separator, field_value = line.partition(':')
        field = NAMED_FIELDS.get(label.strip().casefold())
        if not separator or field is None:
            raise ValidationError(f'Wiersz {index + 1}: nieznana etykieta pola zgłoszenia „{label.strip()}”.')
        if field in data:
            raise ValidationError(f'Wiersz {index + 1}: pole „{label.strip()}” występuje więcej niż raz.')
        if field == 'author_message':
            message = [field_value.lstrip(), *lines[index + 1:]]
            stop = next((i for i, text in enumerate(message) if text.strip() == AUTHOR_MESSAGE_END), len(message))
            data[field] = '\n'.join(message[:stop]).strip()
            break
        data[field] = field_value.strip()
    missing = [label for label, field in NAMED_FIELDS.items() if field in NAMED_REQUIRED_FIELDS and not data.get(field)]
    if missing:
        raise ValidationError(f'Wiersz {first + 1}: uzupełnij wymagane pola: ' + ', '.join(missing) + '.')
    # The email also distinguishes our internal split-name CSV from legacy rows.
    try:
        data['email'] = Review._meta.get_field('email').clean(normalize_email(data['email']), None)
    except ValidationError as error:
        raise ValidationError(f'Wiersz {first + 1}, adres e-mail: ' + ' '.join(error.messages)) from None
    fields = [data.get(field, '') for field in (
        'author_first_name', 'author_last_name', 'author_pseudonym', 'title',
        'genre', 'content_warnings', 'length', 'email', 'phone_number', 'choices', 'author_message',
    )]
    return first + 1, fields


def is_named_record(parts):
    return len(parts) == 11 and '@' in parts[7]

REVIEW_IMPORT_LINE_PATTERN = re.compile(
    r"^\s*\[([^\]]*)\]\s*;\s*"
    r"\[([^\]]*)\]\s*;\s*"
    r"\[([^\]]*)\]\s*;\s*"
    r"\[([^\]]*)\]\s*;\s*"
    r"\[([^\]]*)\]\s*;\s*"
    r"\[([^\]]*)\]\s*;\s*"
    r"\[([^\]]*)\]\s*$"
)


def parse_review_records(value):
    parsed_records = []
    errors = []
    nonempty_count = 0

    for line_number, raw_line, fields in submission_rows(value):
        nonempty_count += 1
        if nonempty_count > MAX_IMPORT_RECORDS:
            raise ValidationError(f"Import może zawierać najwyżej {MAX_IMPORT_RECORDS} niepustych wierszy.")
        match = REVIEW_IMPORT_LINE_PATTERN.fullmatch(raw_line.strip())
        parts = list(match.groups()) if match else fields
        named = is_named_record(parts)
        parts = [part.strip() if named else unbracket(part.strip()) for part in parts]
        consents = {'premieres': False, 'recruitment': False}
        author_message = ''
        has_consent_fields = len(parts) >= 9
        source_anthology = ''
        author_pseudonym = ''
        if named:
            first_name, last_name, author_pseudonym, title, genre, content_warnings, length, email, phone_number, choices, author_message = parts
            author_name = upper(f'{first_name} {last_name}')
            name_parts = [first_name, last_name]
            try:
                consents = parse_consents(choices)
            except ValidationError as error:
                errors.append(f"Wiersz {line_number}: " + ' '.join(error.messages))
                continue
        elif is_legacy_mail_record(parts):
            author_name, title, genre, length, email, phone_number, source_anthology = parts[:7]
            content_warnings = ''
            has_consent_fields = len(parts) >= 8
            try:
                consents = parse_consents(parts[7] if has_consent_fields else '')
            except ValidationError as error:
                errors.append(f"Wiersz {line_number}: " + ' '.join(error.messages))
                continue
            if len(parts) > 8 and any(parts[8:]):
                errors.append(f"Wiersz {line_number}: starszy format zawiera dodatkowe pola. Użyj nowego formatu dla wiadomości autora.")
                continue
        elif len(parts) == 7:
            author_name, title, genre, length, content_warnings, email, phone_number = parts
        elif len(parts) >= 9:
            author_name, title, genre, content_warnings, length, email, phone_number, choices = parts[:8]
            author_message = unbracket(';'.join(parts[8:]).strip())
            try:
                consents = parse_consents(choices)
            except ValidationError as error:
                errors.append(f"Wiersz {line_number}: " + ' '.join(error.messages))
                continue
        else:
            errors.append(f"Wiersz {line_number}: nieprawidłowy format lub liczba pól. Oczekiwano 9 pól albo starszego formatu z 7 lub 8 polami.")
            continue

        author_name = upper(author_name)
        if not named:
            name_parts = author_name.split(maxsplit=1)

        if len(name_parts) != 2:
            errors.append(
                f"Wiersz {line_number}: autora zapisz "
                "jako IMIĘ NAZWISKO."
            )
            continue

        normalized_length = "".join(length.split())

        if (
            not normalized_length
            or not normalized_length.isascii()
            or not normalized_length.isdecimal()
            or len(normalized_length) > 10
        ):
            errors.append(
                f"Wiersz {line_number}: długość musi być "
                "dodatnią liczbą całkowitą."
            )
            continue

        length_value = int(normalized_length)

        if not 1 <= length_value <= 2147483647:
            errors.append(
                f"Wiersz {line_number}: długość jest poza "
                "dozwolonym zakresem."
            )
            continue

        record = {
            "line_number": line_number,
            "author_name": author_name,
            "author_first_name": name_parts[0],
            "author_last_name": name_parts[1],
            "author_pseudonym": normalize_whitespace(author_pseudonym),
            "title": normalize_whitespace(title),
            "genre": normalize_whitespace(genre),
            "length": length_value,
            "content_warnings": content_warnings,
            "email": normalize_email(email),
            "phone_number": phone_number,
            "author_message": author_message,
            "newsletter_premieres": consents['premieres'],
            "newsletter_recruitment": consents['recruitment'],
            "has_consent_fields": has_consent_fields,
            "source_anthology": normalize_whitespace(source_anthology),
        }

        for field_name, normalize in REVIEW_FIELDS.items():
            record[field_name] = normalize(record[field_name])
        record["content_warnings"] = record["content_warnings"].lower()

        row_errors = []

        for field_name in (
            "author_first_name",
            "author_last_name",
            "author_pseudonym",
            "title",
            "genre",
            "length",
            "content_warnings",
            "email",
            "phone_number",
            "author_message",
        ):
            model_field = Review._meta.get_field(field_name)

            try:
                record[field_name] = model_field.clean(
                    record[field_name],
                    None,
                )
            except ValidationError as error:
                row_errors.append(
                    f"Wiersz {line_number}, {model_field.verbose_name}: "
                    + " ".join(error.messages)
                )

        if row_errors:
            errors.extend(row_errors)
        else:
            parsed_records.append(record)

    if not nonempty_count:
        errors.append("Wklej przynajmniej jedno zgłoszenie.")

    return parsed_records, errors


def unbracket(value):
    return value[1:-1] if value.startswith('[') and value.endswith(']') else value


def submission_rows(value):
    """CSV supports quoted multiline messages; line numbers refer to source rows."""
    named = named_submission(value)
    if named is not None:
        start, fields = named
        yield start, value.strip(), fields
        return
    value = clean_pasted_submission(value)
    lines = value.splitlines()
    reader = csv.reader(StringIO(value), delimiter=';', strict=True)
    try:
        while True:
            start = reader.line_num + 1
            try:
                fields = next(reader)
            except StopIteration:
                break
            raw = '\n'.join(lines[start - 1:reader.line_num])
            if raw.strip():
                yield start, raw, fields
    except csv.Error:
        raise ValidationError(f'Wiersz {reader.line_num}: niepoprawny zapis cudzysłowów. Wiadomość wielowierszową zapisz w cudzysłowach CSV; wewnętrzne cudzysłowy podwój.') from None


def encode_submission(fields):
    stream = StringIO(newline='')
    csv.writer(stream, delimiter=';', lineterminator='\n').writerow(fields)
    return stream.getvalue().rstrip('\n')


def clean_pasted_submission(value):
    # Keep visible link text, never use mailto targets as submission data.
    value = re.sub(r'\[([^\]\r\n]*)\]\(mailto:[^\s)]*\)', lambda match: match.group(1), value, flags=re.IGNORECASE)
    value = value.replace('&#x20;', ' ').replace('&#32;', ' ')
    return re.sub(r'\\[ \t]*(?=\r?$)', '', value, flags=re.MULTILINE)


def is_legacy_mail_record(parts):
    return len(parts) >= 7 and '@' in parts[4] and ''.join(parts[3].split()).isascii() and ''.join(parts[3].split()).isdecimal()


def mail_submission_rows(value):
    """Use the same CSV dialect and legacy detection as the manual import.

    The index refers to the last physical line consumed by this CSV record,
    so a quoted multiline message is never appended twice.
    """
    for start, raw, fields in submission_rows(value):
        if is_named_record(fields):
            yield 'named', start + len(raw.splitlines()) - 2, fields
            continue
        parts = [unbracket(field.strip()) if index < 8 else field for index, field in enumerate(fields)]
        if len(parts) >= 8 and '@' in parts[5] and ''.join(parts[4].split()).isascii() and ''.join(parts[4].split()).isdecimal():
            yield 'new', start + len(raw.splitlines()) - 2, parts
        elif is_legacy_mail_record(parts):
            yield 'old', start + len(raw.splitlines()) - 2, parts
