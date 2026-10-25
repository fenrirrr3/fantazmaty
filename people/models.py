from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from core.edit_versions import VersionedQuerySet
from people.roles import RETIRED_ROLES


def get_local_date(value):
    if timezone.is_aware(value):
        value = timezone.localtime(value)

    return value.date()


def format_local_datetime(value):
    if timezone.is_aware(value):
        value = timezone.localtime(value)

    return value.strftime("%d.%m.%Y, %H:%M")


def normalize_datetime(value):
    """Data i godzina ze strefą czasową (USE_TZ = True); wartość bez strefy – w strefie bieżącej."""
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


class Role(models.Model):
    objects = VersionedQuerySet.as_manager()
    def clean(self):
        super().clean()
        if self.name.strip().casefold() in {name.casefold() for name in RETIRED_ROLES}:
            raise ValidationError({"name": "Ogólna rola została wycofana. Wybierz koordynatora konkretnego działu."})

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


class PersonQuerySet(VersionedQuerySet):
    def active(self):
        return self.filter(is_active=True, is_external=False).filter(
            models.Q(user__isnull=True) | models.Q(user__is_active=True)
        )


class Person(models.Model):
    legacy_coordinator_access = models.BooleanField(default=False, editable=False)
    is_external = models.BooleanField('Zewnętrzny', default=False, db_index=True,
        help_text='Widoczny w zespole, bez logowania i udziału w raportach aktywności. Przypisania historyczne pozostają.')
    author_profile = models.OneToOneField("authors.Author", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="team_profile", verbose_name="profil autora", help_text="Opcjonalne, jawne powiązanie. E-mail i pseudonim nie zastępują identyfikatora osoby.")

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
        blank=True,
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

    def save(self, *args, **kwargs):
        if self.is_external:
            self.is_active = False
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'is_active'}
        return super().save(*args, **kwargs)

    @property
    def can_show_team_contact(self):
        return self.is_active and (self.user_id is None or self.user.is_active)


class VacationQuerySet(VersionedQuerySet):
    def unfinished(self, now=None):
        """Urlopy niezakończone w chwili `now`: do odwołania albo z końcem później."""
        return self.filter(models.Q(until_revoked=True) | models.Q(end_date__gt=now or timezone.now()))

    def active(self, now=None):
        """Urlopy trwające w chwili `now`: rozpoczęte najpóźniej tego dnia i niezakończone."""
        now = now or timezone.now()
        return self.unfinished(now).filter(start_date__lte=get_local_date(now))


def remember_person_row(sender, instance, using=None, **kwargs):
    """Poprzedni stan wiersza osoby – jeden odczyt na zapis dla odbiorców z people.signals (koordynacja),
    people.identity (konto) i people.dropbox_access (dostęp do folderów). Pierwszy odbiorca pre_save osoby
    (podłączany w PeopleConfig.ready)."""
    instance._previous_row = sender.objects.using(using).filter(pk=instance.pk).values(
        "is_coordinator", "user_id", "is_active", "is_external", "dropbox_email").first() if instance.pk else None


def remember_role_people(sender, instance, **kwargs):
    """Dotychczasowa nazwa roli i jej osoby przed zapisem albo usunięciem – jeden odczyt dla people.signals
    i people.identity (podłączany w PeopleConfig.ready przed ich odbiorcami)."""
    instance._previous_name = sender.objects.filter(pk=instance.pk).values_list("name", flat=True).first() if instance.pk else None
    instance._role_people = list(instance.people.values_list("pk", flat=True)) if instance.pk else []


class Vacation(models.Model):
    objects = VacationQuerySet.as_manager()
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

        if self.person_id and self.start_date and not errors:
            from datetime import datetime, time
            start = timezone.make_aware(datetime.combine(self.start_date, time.min))
            overlapping = Vacation.objects.filter(person_id=self.person_id).exclude(pk=self.pk).unfinished(start)
            if not self.until_revoked and self.end_date:
                end = normalize_datetime(self.end_date)
                last_day = get_local_date(end)
                if timezone.localtime(end).time() == time.min:
                    from datetime import timedelta
                    last_day -= timedelta(days=1)
                overlapping = overlapping.filter(start_date__lte=last_day)
            if overlapping.exists():
                errors["start_date"] = "Ten okres pokrywa się z twoim innym urlopem."

        if errors:
            raise ValidationError(errors)

    @property
    def is_active(self):
        now = timezone.now()
        if not self.start_date or self.start_date > get_local_date(now):
            return False
        return self.until_revoked or bool(self.end_date and now < normalize_datetime(self.end_date))

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


def normalize_dropbox_path(value):
    """Ścieżka Dropboxa w jednej postaci: ukośnik na początku, bez podwójnych i końcowych ukośników."""
    parts = [part.strip() for part in (value or "").replace("\\", "/").split("/")]
    return "/" + "/".join(part for part in parts if part)


class DropboxFolder(models.Model):
    """Folder Dropboxa, do którego CMS nadaje dostęp członkom zespołu (konto aplikacji jest jego właścicielem)."""

    name = models.CharField("nazwa", max_length=200)
    path = models.CharField(
        "ścieżka w Dropboxie", max_length=500, unique=True,
        help_text="Np. /Fantazmaty/test CMS. Wielkość liter nie ma znaczenia.")
    shared_folder_id = models.CharField(
        "identyfikator udostępnienia", max_length=100, blank=True, editable=False,
        help_text="Uzupełniany automatycznie przy pierwszym nadaniu dostępu.")
    is_active = models.BooleanField(
        "do wyboru przy osobach", default=True,
        help_text="Wyłączony folder nie pojawia się przy nowych osobach; nadane dostępy zostają bez zmian.")

    class Meta:
        verbose_name = "folder Dropbox"
        verbose_name_plural = "foldery Dropbox"
        ordering = ("name", "pk")

    def __str__(self):
        return f"{self.name} ({self.path})"

    def clean(self):
        super().clean()
        self.path = normalize_dropbox_path(self.path)
        if self.path == "/":
            raise ValidationError({"path": "Podaj ścieżkę folderu, np. /Fantazmaty/test CMS."})
        if DropboxFolder.objects.filter(path__iexact=self.path).exclude(pk=self.pk).exists():
            raise ValidationError({"path": "Ten folder jest już na liście."})

    def save(self, *args, **kwargs):
        self.path = normalize_dropbox_path(self.path)
        if self.pk:
            old = DropboxFolder.objects.filter(pk=self.pk).values_list("path", flat=True).first()
            # Inna ścieżka to inny folder – identyfikator udostępnienia zostanie pobrany na nowo.
            if old is not None and old.casefold() != self.path.casefold():
                self.shared_folder_id = ""
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {"shared_folder_id"}
        super().save(*args, **kwargs)


class DropboxAccess(models.Model):
    """Dostęp osoby do folderu Dropbox nadawany przez CMS.

    „wanted” to decyzja w CMS-ie (nadać albo odebrać), „granted_email” – adres, dla którego dostęp naprawdę nadano
    w Dropboxie (CMS usuwa z folderu wyłącznie ten adres). Wiersz osoby usuniętej z bazy zostaje bez osoby,
    dopóki dostęp nie zostanie odebrany w Dropboxie.
    """

    class Level(models.TextChoices):
        EDITOR = "editor", "Edycja"
        VIEWER = "viewer", "Odczyt"

    class State(models.TextChoices):
        PENDING = "pending", "Oczekuje na synchronizację"
        GRANTED = "granted", "Nadany"
        INVITED = "invited", "Zaproszenie wysłane (brak konta Dropbox)"
        TO_REMOVE = "to_remove", "Do usunięcia"
        REMOVED = "removed", "Usunięty"
        ERROR = "error", "Błąd"

    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="dropbox_accesses", verbose_name="osoba")
    folder = models.ForeignKey(
        DropboxFolder, on_delete=models.PROTECT, related_name="accesses", verbose_name="folder")
    level = models.CharField("poziom", max_length=10, choices=Level.choices, default=Level.EDITOR)
    wanted = models.BooleanField("dostęp ma być nadany", default=True)
    state = models.CharField("stan", max_length=20, choices=State.choices, default=State.PENDING)
    granted_email = models.EmailField(
        "adres z dostępem", blank=True,
        help_text="Adres, dla którego CMS nadał dostęp w Dropboxie (przy zmianie adresu ten zostanie usunięty).")
    last_error = models.TextField("ostatni błąd", blank=True)
    synced_at = models.DateTimeField("ostatnia synchronizacja", null=True, blank=True)
    updated_at = models.DateTimeField("zmieniono", auto_now=True)

    class Meta:
        verbose_name = "dostęp do folderu Dropbox"
        verbose_name_plural = "dostęp do folderów Dropbox"
        ordering = ("folder__name", "person__last_name", "person__first_name", "pk")
        constraints = [
            models.UniqueConstraint(fields=("person", "folder"), name="dropbox_access_person_folder"),
        ]

    def __str__(self):
        person = str(self.person) if self.person_id else f"osoba usunięta ({self.granted_email or 'bez adresu'})"
        return f"{person} – {self.folder.name}"
