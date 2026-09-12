from django.conf import settings
from django.db import models
from django.utils import timezone
from core.normalization import AUTHOR_FIELDS, NormalizedModelMixin


class Author(NormalizedModelMixin, models.Model):
    normalization_fields = {**AUTHOR_FIELDS, "email": lambda value: None if value is None else AUTHOR_FIELDS["email"](value)}
    first_name = models.CharField(
        "imię",
        max_length=100,
    )

    last_name = models.CharField(
        "nazwisko",
        max_length=100,
    )

    pseudonym = models.CharField(
        "pseudonim",
        max_length=100,
        blank=True,
    )

    email = models.EmailField(
        "adres e-mail",
        unique=True,
        null=True,
    )

    has_contract = models.BooleanField(
        "umowa",
        default=False,
    )

    contact = models.BooleanField(
        "kontakt",
        default=True,
    )

    # Widoczność i możliwość edycji wyłącznie w panelu administracyjnym
    # muszą być egzekwowane w adminie, formularzach i widokach.
    is_blacklisted = models.BooleanField(
        "czarna lista",
        default=False,
        help_text=(
            "Przed dodaniem zgłoszenia tego autora do recenzji "
            "należy wyświetlić ostrzeżenie."
        ),
    )

    class Meta:
        verbose_name = "autor"
        verbose_name_plural = "autorzy"
        ordering = (
            "last_name",
            "first_name",
            "pk",
        )

    def __str__(self):
        return f"{self.first_name} {self.last_name}".strip()


class BlacklistedAuthor(Author):
    class Meta:
        proxy = True
        verbose_name = "autor na czarnej liście"
        verbose_name_plural = "czarna lista autorów"


class AuthorNote(models.Model):
    author = models.ForeignKey(
        Author,
        on_delete=models.CASCADE,
        related_name="notes",
        verbose_name="autor",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="author_notes",
        verbose_name="osoba dodająca",
        null=True,
        blank=True,
    )

    content = models.TextField(
        "treść notatki",
    )

    created_at = models.DateTimeField(
        "data dodania",
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        verbose_name = "notatka o autorze"
        verbose_name_plural = "notatki o autorach"
        ordering = (
            "-created_at",
            "-pk",
        )

    def __str__(self):
        creator_name = "nieznany autor notatki"

        if self.created_by_id is not None:
            creator_name = (
                self.created_by.get_full_name()
                or self.created_by.get_username()
            )

        if self.created_at is None:
            formatted_date = "bez daty"
        else:
            created_at = self.created_at

            if timezone.is_aware(created_at):
                created_at = timezone.localtime(created_at)

            formatted_date = created_at.strftime("%d.%m.%Y %H:%M")

        return f"{self.author} – {creator_name} – {formatted_date}"
