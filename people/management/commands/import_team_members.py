"""Atomowy i powtarzalny import aktywnych członków zespołu z TSV."""
import csv
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from people.models import Person, Role


HEADERS = ("Nazwisko i imię", "E-mail", "E-mail Dropbox", "Funkcja")
SUPERUSER_MARKER = "Superuser"
COORDINATOR_PREFIX = "Koordynator "
ALLOWED_FUNCTIONS = {
    "Recenzent",
    "Redaktor",
    "Korektor",
    "Weryfikator",
    "Korektor poskładowy",
    "Grafik",
    "Dźwiękowiec",
    "Lektor",
    "Składacz",
    "Tłumacz",
    "Korektor audiobooków",
    "Koordynator redakcji",
    "Koordynator audiobooków",
    "Koordynator weryfikacji",
    "Koordynator ilustracji",
    "Koordynator recenzji",
    "Koordynator korekty",
    "Koordynator rekrutacji",
}


def normalize(value):
    return " ".join((value or "").split())


def normalize_email(value):
    return normalize(value).casefold()


def split_name(value):
    """Źródło przechowuje nazwisko przed imieniem."""
    parts = normalize(value).split(" ", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"Nie można rozdzielić nazwiska i imienia: {value!r}."
        )
    last_name, first_name = parts
    return first_name, last_name


def single(queryset, description):
    matches = list(queryset.order_by("pk")[:2])
    if len(matches) > 1:
        raise ValueError(f"Niejednoznaczny rekord: {description}.")
    return matches[0] if matches else None


class Command(BaseCommand):
    help = (
        "Importuje aktywny zespół z TSV; domyślnie wykonuje podgląd, "
        "a zapis wymaga --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "file",
            nargs="?",
            default=str(
                Path(settings.BASE_DIR)
                / "import_data"
                / "czlonkowie_zespolu.tsv"
            ),
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        self.counts = Counter()
        self.current_row = "?"
        try:
            records = self.read_records(Path(options["file"]))
            with transaction.atomic():
                roles = self.ensure_roles(records)
                for record in records:
                    self.current_row = record["source_rows"]
                    self.import_member(record, roles)
                if not options["commit"]:
                    transaction.set_rollback(True)
        except (
            OSError,
            ValueError,
            ValidationError,
            IntegrityError,
        ) as exc:
            raise CommandError(
                f"Wiersz {self.current_row}: {exc} Cały import wycofano."
            ) from exc

        prefix = "ZAPISANO" if options["commit"] else "PODGLĄD — bez zapisu"
        self.stdout.write(self.style.SUCCESS(prefix))
        self.stdout.write(
            ", ".join(
                f"{name}: {value}"
                for name, value in sorted(self.counts.items())
            )
        )
        self.stdout.write(
            "Nowe konta mają nieużywalne hasło. Ustaw je indywidualnie "
            "poleceniem: python manage.py changepassword ADRES_E_MAIL"
        )

    def read_records(self, path):
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, delimiter="\t")
            if tuple(reader.fieldnames or ()) != HEADERS:
                raise ValueError(
                    "Nieprawidłowe nagłówki. Oczekiwano: "
                    + " | ".join(HEADERS)
                    + "."
                )

            by_email = {}
            for row_number, raw in enumerate(reader, 2):
                if not any(normalize(value) for value in raw.values()):
                    continue
                if None in raw:
                    raise ValueError(
                        f"Wiersz {row_number} ma więcej niż cztery kolumny."
                    )

                row = {name: normalize(raw[name]) for name in HEADERS}
                email = normalize_email(row["E-mail"])
                dropbox_email = normalize_email(row["E-mail Dropbox"])
                function = row["Funkcja"]

                if not email:
                    raise ValueError(f"Brak e-maila w wierszu {row_number}.")
                validate_email(email)
                if dropbox_email:
                    validate_email(dropbox_email)
                if function != SUPERUSER_MARKER and function not in ALLOWED_FUNCTIONS:
                    raise ValueError(
                        f"Nieznana funkcja {function!r} w wierszu {row_number}."
                    )

                first_name, last_name = split_name(row["Nazwisko i imię"])
                member = by_email.setdefault(
                    email,
                    {
                        "first_name": first_name,
                        "last_name": last_name,
                        "email": email,
                        "dropbox_email": dropbox_email,
                        "roles": set(),
                        "is_superuser": False,
                        "source_rows": [],
                    },
                )
                if (
                    member["first_name"] != first_name
                    or member["last_name"] != last_name
                    or member["dropbox_email"] != dropbox_email
                ):
                    raise ValueError(
                        f"Sprzeczne dane adresu {email} w wierszu {row_number}."
                    )
                member["source_rows"].append(row_number)
                if function == SUPERUSER_MARKER:
                    member["is_superuser"] = True
                elif function in member["roles"]:
                    self.counts["pominiete_duplikaty_funkcji"] += 1
                else:
                    member["roles"].add(function)

        if not by_email:
            raise ValueError("Plik nie zawiera żadnych członków zespołu.")
        self.counts["osoby_w_pliku"] = len(by_email)
        return list(by_email.values())

    def ensure_roles(self, records):
        result = {}
        names = sorted({name for record in records for name in record["roles"]})
        for name in names:
            role = single(Role.objects.filter(name__iexact=name), f"rola {name}")
            if role is None:
                role = Role(name=name)
                role.full_clean()
                role.save()
                self.counts["nowe_role"] += 1
            elif role.name != name:
                raise ValueError(
                    f"Istniejąca rola {role.name!r} ma inną pisownię niż {name!r}."
                )
            result[name] = role
        return result

    def find_person(self, record):
        person = single(
            Person.objects.filter(email__iexact=record["email"]),
            f"e-mail osoby {record['email']}",
        )
        if person is not None:
            if (
                normalize(person.first_name).casefold()
                != record["first_name"].casefold()
                or normalize(person.last_name).casefold()
                != record["last_name"].casefold()
            ):
                raise ValueError(
                    f"E-mail {record['email']} należy do innej osoby: {person}."
                )
            return person

        return single(
            Person.objects.filter(
                first_name__iexact=record["first_name"],
                last_name__iexact=record["last_name"],
            ),
            f"osoba {record['first_name']} {record['last_name']}",
        )

    def find_user(self, person, record):
        User = get_user_model()
        if person is not None and person.user_id:
            return person.user

        by_username = single(
            User.objects.filter(username__iexact=record["email"]),
            f"login {record['email']}",
        )
        by_email = single(
            User.objects.filter(email__iexact=record["email"]),
            f"e-mail konta {record['email']}",
        )
        if by_username and by_email and by_username.pk != by_email.pk:
            raise ValueError(
                f"Login i e-mail {record['email']} wskazują różne konta."
            )
        user = by_username or by_email
        if user and Person.objects.filter(user=user).exclude(
            pk=getattr(person, "pk", None),
        ).exists():
            raise ValueError(f"Konto {user} jest powiązane z inną osobą.")
        return user

    def import_member(self, record, roles):
        User = get_user_model()
        person = self.find_person(record)
        user = self.find_user(person, record)

        if user is None:
            user = User(
                username=record["email"],
                email=record["email"],
                first_name=record["first_name"],
                last_name=record["last_name"],
                is_active=True,
                is_staff=record["is_superuser"],
                is_superuser=record["is_superuser"],
            )
            user.set_unusable_password()
            user.full_clean()
            user.save()
            self.counts["nowe_konta"] += 1
        else:
            changes = []
            if user.email and normalize_email(user.email) != record["email"]:
                raise ValueError(
                    f"Powiązane konto {user} ma inny e-mail: {user.email}."
                )
            for field, value in (
                ("email", record["email"]),
                ("first_name", record["first_name"]),
                ("last_name", record["last_name"]),
                ("is_active", True),
            ):
                if getattr(user, field) != value:
                    setattr(user, field, value)
                    changes.append(field)
            if record["is_superuser"]:
                for field in ("is_staff", "is_superuser"):
                    if not getattr(user, field):
                        setattr(user, field, True)
                        changes.append(field)
            if changes:
                user.full_clean()
                user.save(update_fields=changes)
                self.counts["zaktualizowane_konta"] += 1

        is_coordinator = any(
            name.startswith(COORDINATOR_PREFIX)
            for name in record["roles"]
        )
        if person is None:
            person = Person(
                first_name=record["first_name"],
                last_name=record["last_name"],
                email=record["email"],
                dropbox_email=record["dropbox_email"],
                is_active=True,
                is_coordinator=is_coordinator,
                user=user,
            )
            person.full_clean()
            person.save()
            self.counts["nowe_osoby"] += 1
        else:
            changes = []
            old_email = normalize_email(person.email)
            if old_email and old_email != record["email"]:
                old_line = f"Poprzedni e-mail: {person.email}"
                if old_line not in person.previous_data:
                    person.previous_data = "\n".join(
                        filter(None, (person.previous_data.strip(), old_line))
                    )
                    changes.append("previous_data")
            for field, value in (
                ("first_name", record["first_name"]),
                ("last_name", record["last_name"]),
                ("email", record["email"]),
                ("dropbox_email", record["dropbox_email"]),
                ("is_active", True),
                ("user", user),
            ):
                if getattr(person, field) != value:
                    setattr(person, field, value)
                    changes.append(field)
            if is_coordinator and not person.is_coordinator:
                person.is_coordinator = True
                changes.append("is_coordinator")
            if changes:
                person.full_clean()
                person.save(update_fields=list(dict.fromkeys(changes)))
                self.counts["zaktualizowane_osoby"] += 1

        before = set(person.roles.values_list("name", flat=True))
        person.roles.add(*(roles[name] for name in record["roles"]))
        self.counts["nowe_przypisania_funkcji"] += len(record["roles"] - before)
