"""Wspólne eksporty CSV z ochroną przed interpretacją danych jako formuł."""

import csv
import unicodedata
from datetime import date, datetime

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header

from texts.models import Review, Text

from .permissions import can_export_author_data


FORMULA_PREFIXES = frozenset({"=", "+", "-", "@"})


def safe_csv_value(value):
    """Przygotowuje wartość komórki, zachowując jej oryginalną treść.

    Apostrof jest dodawany przed potencjalną formułą. Samo cytowanie
    komórki przez csv.writer nie zabezpiecza przed formułami arkusza.
    """
    if value is None:
        return ""

    if isinstance(value, bool):
        return "Tak" if value else "Nie"

    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)

        value = value.isoformat(sep=" ", timespec="seconds")
    elif isinstance(value, date):
        value = value.isoformat()
    else:
        value = str(value)

    if not value:
        return ""

    # Normalizacja służy wyłącznie wykryciu ryzyka.
    # Nie zmieniamy zapisywanej treści, odstępów ani znaków Unicode.
    inspected = unicodedata.normalize("NFKC", value)
    position = 0

    while position < len(inspected):
        character = inspected[position]

        if (
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf"}
        ):
            position += 1
        else:
            break

    starts_with_control = unicodedata.category(value[0]) in {"Cc", "Cf"}
    starts_with_formula = (
        position < len(inspected)
        and inspected[position] in FORMULA_PREFIXES
    )

    if starts_with_control or starts_with_formula:
        return f"'{value}"

    return value


def _safe_filename(filename):
    filename = str(filename or "eksport.csv")
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(
        character
        for character in filename
        if not unicodedata.category(character).startswith("C")
    ).strip()

    if filename.lower().endswith(".csv"):
        filename = filename[:-4]

    filename = filename.strip(" .")[:160] or "eksport"
    return f"{filename}.csv"


def _require_export_access(user):
    if not can_export_author_data(user):
        raise PermissionDenied(
            "Eksport zawierający dane autorów jest dostępny "
            "wyłącznie dla superusera."
        )


def csv_response(*, user, headers, rows, filename="eksport.csv"):
    """Buduje odpowiedź CSV, sprawdzając dostęp przed odczytem wierszy.

    Eksport jest wykonywany w trakcie obsługi żądania, dzięki czemu
    zapytania nie są odraczane poza transakcję wywołującego widoku.
    """
    _require_export_access(user)

    response = HttpResponse(
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True,
        filename=_safe_filename(filename),
    )
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"

    # BOM umożliwia poprawne rozpoznanie polskich znaków przez Excel.
    response.write("\ufeff")

    writer = csv.writer(
        response,
        delimiter=";",
        quotechar='"',
        quoting=csv.QUOTE_ALL,
        doublequote=True,
        lineterminator="\r\n",
    )
    writer.writerow(safe_csv_value(value) for value in headers)

    for row in rows:
        writer.writerow(safe_csv_value(value) for value in row)

    return response


def export_texts_csv(user, texts, filename="teksty.csv"):
    """Eksportuje wyłącznie przekazany zestaw tekstów.

    Zachowuje istniejący układ kolumn i zastosowane wcześniej filtry.
    """
    _require_export_access(user)

    if isinstance(texts, QuerySet):
        if texts.model is not Text:
            raise TypeError("Oczekiwano QuerySetu modelu Text.")

        texts = (
            texts.select_related("anthology")
            .prefetch_related("authors")
        )

        if not texts.ordered:
            texts = texts.order_by("pk")

        texts = texts.iterator(chunk_size=500)

    def rows():
        for text in texts:
            yield (
                text.anthology.title if text.anthology_id else "",
                text.authors_display,
                text.title,
                text.length,
            )

    return csv_response(
        user=user,
        headers=("Antologia", "Autorzy", "Tytuł", "Długość"),
        rows=rows(),
        filename=filename,
    )


def export_reviews_csv(user, reviews, filename="recenzje.csv"):
    """Eksportuje wyłącznie przekazany zestaw recenzji.

    Widok ustala zakres, w tym ewentualne wykluczenie old_reviews.
    Eksporter nie rozszerza ani nie zmienia wyboru użytkownika.
    """
    _require_export_access(user)

    if isinstance(reviews, QuerySet):
        if reviews.model is not Review:
            raise TypeError("Oczekiwano QuerySetu modelu Review.")

        reviews = reviews.select_related("anthology")

        if not reviews.ordered:
            reviews = reviews.order_by("pk")

        reviews = reviews.iterator(chunk_size=500)

    def rows():
        for review in reviews:
            yield (
                review.anthology.title,
                (
                    f"{review.author_first_name} "
                    f"{review.author_last_name}"
                ).strip(),
                review.title,
                review.get_status_display(),
            )

    return csv_response(
        user=user,
        headers=("Nabór", "Autor", "Tytuł", "Status"),
        rows=rows(),
        filename=filename,
    )