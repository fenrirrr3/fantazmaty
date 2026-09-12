from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from people.models import Person
from texts.models import Text


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

    illustrator = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="illustrations",
        verbose_name="ilustrator",
        null=True,
        blank=True,
    )

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

        if (
            self.status != self.Status.UNASSIGNED
            and not self.illustrator_id
        ):
            errors["illustrator"] = (
                "Status inny niż „Nieprzypisane” wymaga "
                "wybrania ilustratora."
            )

        if (
            self.status == self.Status.UNASSIGNED
            and self.illustrator_id
        ):
            errors["status"] = (
                "Jeżeli wybrano ilustratora, zmień status "
                "na „Przypisane”."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        previous_status = None

        if self.pk:
            previous_status = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        status_changed_to_assigned = (
            self.status == self.Status.ASSIGNED
            and previous_status != self.Status.ASSIGNED
        )

        if status_changed_to_assigned:
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
    def anthology(self):
        return self.text.anthology

    @property
    def authors_display(self):
        return ", ".join(
            str(author)
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