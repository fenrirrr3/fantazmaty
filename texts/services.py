"""Wspólna kontrola autorów i możliwych duplikatów zgłoszeń.

Funkcje są używane przez panel administracyjny i import recenzji.
Nie zapisują danych i nie zastępują kontroli uprawnień w wywołującym
widoku lub serwisie.

Kontrola obejmuje również old_reviews. Duplikat oznacza zgodność
znormalizowanego tytułu oraz tożsamości autora ustalonej przez
powiązanie z Author lub adres e-mail. Nie porównuje treści utworów.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator

from django.db import models, router

from authors.models import Author

from .models import Review


MAX_DUPLICATES_IN_WARNING = 10


def normalize_whitespace(value: str | None) -> str:
    """Normalizuje Unicode i zastępuje ciągi białych znaków spacją."""
    return " ".join(
        unicodedata.normalize("NFKC", value or "").split()
    )


def normalize_author_name(value: str | None) -> str:
    return normalize_whitespace(value).casefold()


def normalize_review_title(value: str | None) -> str:
    """Zachowuje znaki diakrytyczne i interpunkcję tytułu."""
    return normalize_whitespace(value).casefold()


def normalize_email(value: str | None) -> str:
    """Nie usuwa kropek ani części adresu następującej po znaku +."""
    return unicodedata.normalize(
        "NFKC",
        value or "",
    ).strip().casefold()


def _database_alias(
    author: Author | None,
    using: str | None,
) -> str:
    if using:
        return using

    if author is not None and author._state.db:
        return author._state.db

    return router.db_for_read(Review)


def _identity_emails(
    author: Author | None,
    email: str | None,
) -> tuple[str, ...]:
    values = {normalize_email(email)}

    if author is not None:
        values.add(normalize_email(author.email))

    values.discard("")
    return tuple(sorted(values))


def _validate_author(author: Author | None) -> None:
    if author is not None and (
        author.pk is None or author._state.adding
    ):
        raise ValueError(
            "Autor przekazany do kontroli zgłoszenia musi być zapisany."
        )


def find_matching_authors(
    *,
    author: Author | None = None,
    email: str = "",
    using: str | None = None,
) -> models.QuerySet:
    """Zwraca autorów pasujących przez identyfikator lub adres e-mail.

    Samo podobieństwo imienia i nazwiska nie potwierdza tożsamości.
    Manager nie ogranicza wyników do autorów spoza czarnej listy.
    """
    _validate_author(author)

    database = _database_alias(author, using)
    queryset = Author.objects.using(database)
    conditions = models.Q()

    if author is not None:
        conditions |= models.Q(pk=author.pk)

    for address in _identity_emails(author, email):
        conditions |= models.Q(email__iexact=address)

    if not conditions:
        return queryset.none()

    return queryset.filter(conditions).order_by("pk")


def _candidate_reviews(
    *,
    author: Author | None,
    email: str,
    exclude_review_id: int | None,
    using: str,
) -> models.QuerySet:
    """Ogranicza koszt porównywania tytułów do zgłoszeń danego autora."""
    queryset = Review.objects.using(using)

    matching_author_ids = (
        find_matching_authors(
            author=author,
            email=email,
            using=using,
        )
        .order_by()
        .values("pk")
    )

    conditions = models.Q(author_id__in=matching_author_ids)

    for address in _identity_emails(author, email):
        conditions |= models.Q(email__iexact=address)

    queryset = queryset.filter(conditions)

    if exclude_review_id is not None:
        queryset = queryset.exclude(pk=exclude_review_id)

    # Celowo bez current() ani for_statistics():
    # stare i zamknięte zgłoszenia również mogą być duplikatami.
    return queryset.order_by("pk")


def _iter_duplicate_reviews(
    *,
    author: Author | None,
    email: str,
    title: str,
    exclude_review_id: int | None,
    using: str,
) -> Iterator[Review]:
    normalized_title = normalize_review_title(title)

    if not normalized_title:
        return

    if author is None and not normalize_email(email):
        return

    candidates = _candidate_reviews(
        author=author,
        email=email,
        exclude_review_id=exclude_review_id,
        using=using,
    ).only(
        "pk",
        "title",
        "created_at",
        "old_reviews",
    )

    # Porównanie w Pythonie zapewnia jednakową normalizację Unicode
    # tytułów na SQLite i PostgreSQL.
    for candidate in candidates.iterator(chunk_size=500):
        if normalize_review_title(candidate.title) == normalized_title:
            yield candidate


def find_duplicate_reviews(
    *,
    title: str,
    author: Author | None = None,
    email: str = "",
    exclude_review_id: int | None = None,
    using: str | None = None,
) -> list[Review]:
    """Zwraca możliwe duplikaty, także z innych naborów i archiwum.

    Obiekty zawierają wczytane pola pk, title, created_at i old_reviews.
    Odczyt pozostałych pól może wykonać dodatkowe zapytanie.
    """
    _validate_author(author)
    database = _database_alias(author, using)

    return list(
        _iter_duplicate_reviews(
            author=author,
            email=email,
            title=title,
            exclude_review_id=exclude_review_id,
            using=database,
        )
    )


def get_review_submission_warnings(
    *,
    title: str,
    author: Author | None = None,
    author_first_name: str = "",
    author_last_name: str = "",
    email: str = "",
    exclude_review_id: int | None = None,
    using: str | None = None,
) -> list[str]:
    """Zwraca ostrzeżenia wymagające pokazania przed zapisem.

    Imię i nazwisko należą do wspólnego interfejsu danych zgłoszenia,
    ale nie są samodzielną podstawą utożsamiania dwóch autorów.

    Gdy formularz edytuje istniejące zgłoszenie, musi przekazać jego
    identyfikator jako exclude_review_id.

    Komunikaty mogą zawierać identyfikatory wcześniejszych zgłoszeń.
    Wywołujący musi sprawdzić uprawnienia przed ich udostępnieniem.
    """
    _validate_author(author)
    database = _database_alias(author, using)
    warnings = []

    matching_authors = find_matching_authors(
        author=author,
        email=email,
        using=database,
    )

    # Sprawdzamy aktualny stan bazy, a nie potencjalnie nieaktualną
    # wartość author.is_blacklisted w przekazanym obiekcie.
    blacklisted_ids = list(
        matching_authors.filter(
            is_blacklisted=True,
        ).values_list("pk", flat=True)
    )

    if blacklisted_ids:
        references = ", ".join(
            f"#{author_id}" for author_id in blacklisted_ids
        )
        warnings.append(
            "Autor zgłoszenia pasuje do rekordu na czarnej liście "
            f"({references}). Sprawdź oznaczenie przed dodaniem recenzji."
        )

    duplicate_count = 0
    duplicate_descriptions = []

    for duplicate in _iter_duplicate_reviews(
        author=author,
        email=email,
        title=title,
        exclude_review_id=exclude_review_id,
        using=database,
    ):
        duplicate_count += 1

        if len(duplicate_descriptions) >= MAX_DUPLICATES_IN_WARNING:
            continue

        date_label = (
            duplicate.created_at.strftime("%d.%m.%Y")
            if duplicate.created_at
            else "bez daty"
        )
        archive_label = " – archiwum" if duplicate.old_reviews else ""

        duplicate_descriptions.append(
            f"#{duplicate.pk} z {date_label}{archive_label}"
        )

    if duplicate_count:
        warning = (
            "Możliwy duplikat: ten sam autor ma już zgłoszenie "
            "o takim samym znormalizowanym tytule. "
            f"Liczba pasujących zgłoszeń: {duplicate_count}. "
            "Znalezione rekordy: "
            + "; ".join(duplicate_descriptions)
            + "."
        )

        remaining = duplicate_count - len(duplicate_descriptions)

        if remaining:
            warning += f" Pozostałych pasujących rekordów: {remaining}."

        warning += (
            " Sprawdź, czy jest to ponowne przesłanie tego samego utworu. "
            "Kontrola obejmuje wszystkie nabory i stare recenzje."
        )
        warnings.append(warning)

    return warnings