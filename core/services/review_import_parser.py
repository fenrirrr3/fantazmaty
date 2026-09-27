"""Parse pasted review rows; form-level duplicate/blacklist checks stay in the form."""
import re
from django.core.exceptions import ValidationError
from texts.models import Review
from texts.services import normalize_email, normalize_whitespace
from core.normalization import REVIEW_FIELDS, upper

MAX_IMPORT_RECORDS = 500
MAX_IMPORT_CHARACTERS = 1_000_000
MAX_IMPORT_ERRORS = 20

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

    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()

        if not line:
            continue

        nonempty_count += 1

        if nonempty_count > MAX_IMPORT_RECORDS:
            raise ValidationError(
                f"Import może zawierać najwyżej {MAX_IMPORT_RECORDS} "
                "niepustych wierszy."
            )

        match = REVIEW_IMPORT_LINE_PATTERN.fullmatch(line)
        # Zachowaj zgodność ze starszymi eksportami w nawiasach.
        parts = match.groups() if match else line.split(";")
        if len(parts) != 7:
            errors.append(
                f"Wiersz {line_number}: nieprawidłowy format "
                "lub liczba pól."
            )
            continue

        (
            author_name,
            title,
            genre,
            length,
            content_warnings,
            email,
            phone_number,
        ) = (part.strip() for part in parts)

        author_name = upper(author_name)
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
            "title": normalize_whitespace(title),
            "genre": normalize_whitespace(genre),
            "length": length_value,
            "content_warnings": content_warnings,
            "email": normalize_email(email),
            "phone_number": phone_number,
        }

        for field_name, normalize in REVIEW_FIELDS.items():
            record[field_name] = normalize(record[field_name])
        record["content_warnings"] = record["content_warnings"].lower()

        row_errors = []

        for field_name in (
            "author_first_name",
            "author_last_name",
            "title",
            "genre",
            "length",
            "content_warnings",
            "email",
            "phone_number",
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
