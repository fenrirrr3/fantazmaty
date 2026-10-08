from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.utils import timezone

from texts.models import Text
from .validators import validate_drive_url


class PublicIllustrationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    drive_url = models.URLField('wspólny link GDrive', max_length=1000, blank=True, default='', validators=[validate_drive_url],
        help_text='Link publiczny, pokazywany przy każdym tekście w Zewnętrznych ilustracjach.')

    class Meta:
        verbose_name = 'ustawienia zewnętrznych ilustracji'
        verbose_name_plural = 'Zewnętrzne ilustracje – ustawienia'
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='single_public_illustration_settings')]

    def __str__(self):
        return 'Zewnętrzne ilustracje – wspólny link GDrive'


class Illustrator(models.Model):
    """An independent contact, with no account or team-profile relationship."""
    first_name = models.CharField("imię", max_length=100)
    last_name = models.CharField("nazwisko", max_length=100, blank=True)
    email = models.EmailField("adres e-mail", blank=True, null=True, unique=True)
    portfolio = models.URLField("portfolio", max_length=500, blank=True,
                                validators=[URLValidator(schemes=["http", "https"])])
    preferences = models.TextField("preferencje", blank=True)
    covers = models.BooleanField("okładki", default=False)
    is_active = models.BooleanField("aktywny ilustrator", default=True, db_index=True,
        help_text="Wyłączenie ukrywa wpis w aktywnym spisie i przy nowych przypisaniach. Dotychczasowe prace pozostają.")

    class Meta:
        verbose_name = "wpis ilustratora"
        verbose_name_plural = "Ilustratorzy"
        ordering = ("last_name", "first_name", "pk")

    def __str__(self):
        return f"{self.first_name} {self.last_name}".strip()

    def clean(self):
        super().clean()
        self.first_name = " ".join(self.first_name.split())
        self.last_name = " ".join(self.last_name.split())
        self.email = (self.email or "").strip() or None
        if self.email and type(self).objects.filter(email__iexact=self.email).exclude(pk=self.pk).exists():
            raise ValidationError({"email": "Wpis z tym adresem e-mail już istnieje w spisie ilustratorów."})


class Illustration(models.Model):
    class Status(models.TextChoices):
        UNASSIGNED = (
            "unassigned",
            "Nieprzypisane",
        )
        ASSIGNED = (
            "assigned",
            "Przypisane",
        )
        DELIVERED = (
            "delivered",
            "Oddane",
        )
        IN_CORRECTIONS = (
            "in_corrections",
            "W trakcie poprawek",
        )

    text = models.OneToOneField(
        Text,
        on_delete=models.PROTECT,
        related_name="illustration",
        verbose_name="tytuł",
    )

    illustrators = models.ManyToManyField(
        Illustrator, related_name="illustrations", verbose_name="ilustratorzy", blank=True,
    )

    manual_illustrator_name = models.CharField('Ilustrator – imię i nazwisko (ręcznie)', max_length=255, blank=True)
    manual_illustrator_email = models.EmailField('E-mail ilustratora (ręcznie)', blank=True)
    coordinator_notes = models.TextField('Uwagi koordynatora', blank=True)

    def artist_ids(self):
        if hasattr(self, '_selected_illustrator_ids'):
            return self._selected_illustrator_ids
        return {artist.pk for artist in self.illustrators.all()} if self.pk else set()

    @property
    def illustrator_display(self):
        names = [str(artist) for artist in self.illustrators.all()] if self.pk else []
        return ", ".join(names) or self.manual_illustrator_name

    def set_artists(self, artists, *, status=None, preserve_assignment_date=False):
        """Validate the complete assignment before writing its many-to-many relation."""
        from django.db import transaction
        artists = list(artists)
        self._selected_illustrator_ids = {artist.pk for artist in artists}
        if status is not None:
            self.status = status
        try:
            with transaction.atomic(using=self._state.db):
                self.save(preserve_assignment_date=preserve_assignment_date)
                self.illustrators.set(artists)
        finally:
            del self._selected_illustrator_ids

    trigger_warnings = models.TextField(
        "trigger warnings",
        blank=True,
    )

    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.UNASSIGNED,
        db_index=True,
    )

    assigned_at = models.DateField(
        "data przypisania",
        null=True,
        blank=True,
        editable=False,
    )

    story_url = models.URLField(
        "link do opowiadania",
        max_length=500,
        blank=True,
    )

    illustrated_excerpt = models.TextField(
        "ilustrowany fragment",
        blank=True,
    )

    class Meta:
        verbose_name = "ilustracja"
        verbose_name_plural = "ilustracje"
        ordering = (
            "text__anthology__title",
            "text__title",
            "pk",
        )

    def __str__(self):
        return self.text.title

    def clean(self):
        super().clean()

        errors = {}
        # Existing credits remain editable when their anthology is hidden later.
        previous_text = type(self).objects.using(self._state.db or 'default').filter(pk=self.pk).values_list('text_id', flat=True).first() if self.pk else None
        if self.text_id and self.text_id != previous_text:
            supported = Text.objects.using(self._state.db or 'default').exclude(anthology__is_novel=True).exclude(anthology__is_translated=True)
            if not supported.filter(pk=self.text_id).exists():
                errors['text'] = 'Powieści i tłumaczenia nie obsługują ilustracji.'
        self.manual_illustrator_name = self.manual_illustrator_name.strip()
        self.manual_illustrator_email = self.manual_illustrator_email.strip()
        if self.artist_ids() and (self.manual_illustrator_name or self.manual_illustrator_email):
            errors['manual_illustrator_name'] = 'Wybierz profil z listy albo wpisz osobę ręcznie; nie oba naraz.'
        if self.manual_illustrator_email and not self.manual_illustrator_name:
            errors['manual_illustrator_name'] = 'Podaj imię i nazwisko ilustratora.'
        has_illustrator = bool(self.artist_ids() or self.manual_illustrator_name)

        if (
            self.status != self.Status.UNASSIGNED
            and not has_illustrator
        ):
            errors["status"] = (
                "Status inny niż „Nieprzypisane” wymaga "
                "wybrania ilustratora."
            )

        if (
            self.status == self.Status.UNASSIGNED
            and has_illustrator
        ):
            errors["status"] = (
                "Jeżeli wybrano ilustratora, zmień status "
                "na „Przypisane”."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, preserve_assignment_date=False, **kwargs):
        previous = (type(self).objects.filter(pk=self.pk)
                    .values('status', 'assigned_at', 'manual_illustrator_name', 'manual_illustrator_email').first()) if self.pk else None
        self.manual_illustrator_name = self.manual_illustrator_name.strip()
        self.manual_illustrator_email = self.manual_illustrator_email.strip()
        status_changed_to_assigned = bool(self.artist_ids() or self.manual_illustrator_name) and (
            (previous is None and self.status == self.Status.ASSIGNED)
            or (previous is not None and (
                self.artist_ids() != {artist.pk for artist in self.illustrators.all()}
                or previous['manual_illustrator_name'] != self.manual_illustrator_name
                or previous['manual_illustrator_email'] != self.manual_illustrator_email
                or previous['status'] == self.Status.UNASSIGNED
                or (self.status == self.Status.ASSIGNED and previous['assigned_at'] is None)
            ))
        )

        if status_changed_to_assigned and not preserve_assignment_date:
            self.assigned_at = timezone.localdate()
        elif self.status == self.Status.UNASSIGNED:
            self.assigned_at = None

        self.full_clean()

        update_fields = kwargs.get("update_fields")

        if update_fields is not None:
            updated_fields = set(update_fields)

            if (
                status_changed_to_assigned
                or self.status == self.Status.UNASSIGNED
            ):
                updated_fields.add("assigned_at")

            kwargs["update_fields"] = updated_fields

        super().save(*args, **kwargs)

    @property
    def genre_display(self):
        return self.text.genre

    @property
    def genre_tags_display(self):
        return " · ".join(value for value in (self.text.genre, self.text.tags) if value)

    @property
    def warnings_display(self):
        return self.text.content_warnings or self.trigger_warnings

    @property
    def anthology(self):
        return self.text.anthology

    @property
    def authors_display(self):
        return ", ".join(
            author.display_name
            for author in self.text.authors.all()
        )

class CoverProposal(models.Model):
    class Status(models.TextChoices):
        PENDING = (
            "pending",
            "Oczekujące",
        )
        INQUIRY_SENT = (
            "inquiry_sent",
            "Wysłane zapytanie",
        )
        REJECTED = (
            "rejected",
            "Odrzucone",
        )
        NO_CONTACT = (
            "no_contact",
            "Brak kontaktu",
        )
        APPROVED = (
            "approved",
            "Zgoda",
        )

    illustration_author = models.CharField(
        "autor ilustracji",
        max_length=255,
    )

    illustration_url = models.URLField(
        "link do ilustracji",
        max_length=500,
    )

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="cover_proposals",
        verbose_name="zgłoszone przez",
        null=True,
        editable=False,
    )

    submitted_at = models.DateTimeField(
        "data zgłoszenia",
        auto_now_add=True,
    )

    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    status_changed_at = models.DateTimeField(
        "data zmiany statusu",
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        verbose_name = "propozycja ilustracji okładkowej"
        verbose_name_plural = "propozycje ilustracji okładkowych"
        ordering = (
            "-submitted_at",
            "-pk",
        )

    def __str__(self):
        return (
            f"{self.illustration_author} – "
            f"{self.get_status_display()}"
        )

    def save(self, *args, **kwargs):
        previous_status = None

        if self.pk:
            previous_status = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        status_changed = (
            self.pk is not None
            and previous_status is not None
            and previous_status != self.status
        )

        if status_changed:
            self.status_changed_at = timezone.now()

        update_fields = kwargs.get("update_fields")

        if update_fields is not None:
            updated_fields = set(update_fields)

            if status_changed:
                updated_fields.add("status_changed_at")

            kwargs["update_fields"] = updated_fields

        super().save(*args, **kwargs)
