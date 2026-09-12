from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def get_local_date(value):
    if timezone.is_aware(value):
        value = timezone.localtime(value)

    return value.date()


def format_local_datetime(value):
    if timezone.is_aware(value):
        value = timezone.localtime(value)

    return value.strftime("%d.%m.%Y, %H:%M")


def normalize_datetime(value):
    if settings.USE_TZ and timezone.is_naive(value):
        return timezone.make_aware(
            value,
            timezone.get_current_timezone(),
        )

    if not settings.USE_TZ and timezone.is_aware(value):
        return timezone.make_naive(
            value,
            timezone.get_current_timezone(),
        )

    return value


def leave_is_active(start_date, end_date, until_revoked):
    if not start_date:
        return False

    now = timezone.now()

    if start_date > get_local_date(now):
        return False

    if until_revoked:
        return True

    return bool(
        end_date
        and now < normalize_datetime(end_date)
    )


class Role(models.Model):
    name = models.CharField(
        "nazwa roli",
        max_length=100,
        unique=True,
    )

    class Meta:
        verbose_name = "rola"
        verbose_name_plural = "role"
        ordering = ("name",)

    def __str__(self):
        return self.name


class PersonQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def with_roles(self, role_ids):
        """Osoby mające co najmniej jedną ze wskazanych ról."""
        role_ids = tuple(role_ids)

        if not role_ids:
            return self

        return self.filter(roles__pk__in=role_ids).distinct()


class Person(models.Model):
    first_name = models.CharField(
        "imię",
        max_length=100,
    )

    last_name = models.CharField(
        "nazwisko",
        max_length=100,
    )

    roles = models.ManyToManyField(
        Role,
        related_name="people",
        verbose_name="role",
        blank=True,
    )

    is_coordinator = models.BooleanField(
        "koordynator",
        default=False,
    )

    email = models.EmailField(
        "adres e-mail",
        unique=True,
        null=True,
    )

    dropbox_email = models.EmailField(
        "adres e-mail Dropboxa",
        blank=True,
    )

    previous_data = models.TextField(
        "poprzednie dane",
        blank=True,
        default="",
        help_text=(
            "Dawne imiona i nazwiska, dodatkowe adresy e-mail oraz "
            "wyjaśnienia rozbieżności w danych. Pole dostępne wyłącznie "
            "w panelu administracyjnym."
        ),
    )

    is_active = models.BooleanField(
        "wciąż w ekipie",
        default=True,
    )

    leave_start_date = models.DateField(
        "urlop od",
        null=True,
        blank=True,
    )

    leave_end_date = models.DateTimeField(
        "urlop do",
        null=True,
        blank=True,
    )

    leave_until_revoked = models.BooleanField(
        "urlop do odwołania",
        default=False,
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="person_profile",
        verbose_name="konto użytkownika",
        null=True,
        blank=True,
    )

    # Domyślny manager zachowuje dostęp do dawnych członków zespołu
    # dla administracji oraz historycznych przydziałów.
    # Lista zespołu powinna korzystać z Person.objects.active().
    objects = PersonQuerySet.as_manager()

    class Meta:
        verbose_name = "osoba"
        verbose_name_plural = "osoby"
        ordering = (
            "last_name",
            "first_name",
            "pk",
        )

    def __str__(self):
        return f"{self.first_name} {self.last_name}".strip()

    def clean(self):
        super().clean()

        errors = {}

        if self.leave_end_date and not self.leave_start_date:
            errors["leave_start_date"] = (
                "Podaj datę rozpoczęcia urlopu."
            )

        if self.leave_until_revoked and not self.leave_start_date:
            errors["leave_start_date"] = (
                "Urlop do odwołania wymaga daty rozpoczęcia."
            )

        if self.leave_until_revoked and self.leave_end_date:
            errors["leave_end_date"] = (
                "Urlop do odwołania nie może mieć daty zakończenia."
            )

        if (
            self.leave_start_date
            and not self.leave_until_revoked
            and not self.leave_end_date
        ):
            errors["leave_end_date"] = (
                "Podaj datę i godzinę zakończenia albo zaznacz "
                "„urlop do odwołania”."
            )

        if (
            self.leave_start_date
            and self.leave_end_date
            and get_local_date(self.leave_end_date)
            < self.leave_start_date
        ):
            errors["leave_end_date"] = (
                "Data zakończenia nie może być wcześniejsza "
                "niż data rozpoczęcia."
            )

        if errors:
            raise ValidationError(errors)

    @property
    def is_on_leave(self):
        return leave_is_active(
            self.leave_start_date,
            self.leave_end_date,
            self.leave_until_revoked,
        )


class Vacation(models.Model):
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="vacations",
        verbose_name="członek ekipy",
    )

    start_date = models.DateField(
        "urlop od",
    )

    end_date = models.DateTimeField(
        "urlop do",
        null=True,
        blank=True,
    )

    until_revoked = models.BooleanField(
        "urlop do odwołania",
        default=False,
    )

    created_at = models.DateTimeField(
        "data zgłoszenia",
        auto_now_add=True,
    )

    class Meta:
        verbose_name = "urlop"
        verbose_name_plural = "urlopy"
        ordering = (
            "-start_date",
            "-created_at",
            "-pk",
        )

    def __str__(self):
        if self.start_date:
            period = f"od {self.start_date:%d.%m.%Y}"
        else:
            period = "bez daty rozpoczęcia"

        if self.until_revoked:
            period += " do odwołania"
        elif self.end_date:
            period += f" do {format_local_datetime(self.end_date)}"

        person = (
            str(self.person)
            if self.person_id is not None
            else "bez przypisanej osoby"
        )

        return f"{person}: {period}"

    def clean(self):
        super().clean()

        errors = {}

        if not self.start_date:
            errors["start_date"] = (
                "Podaj datę rozpoczęcia urlopu."
            )

        if self.until_revoked and self.end_date:
            errors["end_date"] = (
                "Urlop do odwołania nie może mieć "
                "daty zakończenia."
            )

        if not self.until_revoked and not self.end_date:
            errors["end_date"] = (
                "Podaj datę i godzinę zakończenia albo zaznacz "
                "„urlop do odwołania”."
            )

        if (
            self.start_date
            and self.end_date
            and get_local_date(self.end_date) < self.start_date
        ):
            errors["end_date"] = (
                "Data zakończenia nie może być wcześniejsza "
                "niż data rozpoczęcia."
            )

        if errors:
            raise ValidationError(errors)

    @property
    def is_active(self):
        return leave_is_active(
            self.start_date,
            self.end_date,
            self.until_revoked,
        )

    @property
    def is_finished(self):
        return bool(
            not self.until_revoked
            and self.end_date
            and normalize_datetime(self.end_date) <= timezone.now()
        )

    @property
    def is_upcoming(self):
        return bool(
            self.start_date
            and self.start_date > get_local_date(timezone.now())
        )

    @property
    def can_be_edited(self):
        return self.is_active or self.is_upcoming

    @property
    def can_be_ended(self):
        return self.is_active
