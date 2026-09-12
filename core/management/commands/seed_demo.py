"""Dodaje fikcyjnych autorów i zespół; ponowne uruchomienie nie nadpisuje danych."""
from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Lower

from authors.models import Author
from people.models import Person, Role

AUTHORS = (
    ("Anna", "Wierzbicka", "A. Wierzba", False),
    ("Piotr", "Lesicki", "", False),
    ("Maria", "Sosnowska", "M. Sosna", False),
    ("Tomasz", "Brzeziński", "", False),
    ("Ewa", "Kalinowska", "Kalina", False),
    ("Michał", "Jaworski", "", False),
    ("Julia", "Grabowska", "J. Grab", False),
    ("Adam", "DemoCzarnalista", "", True),
    ("Beata", "DemoCzarnalista", "", True),
    ("Cezary", "DemoCzarnalista", "", True),
)
TEAM = (
    ("recenzent_1", "Recenzent", "Alicja", "DemoRecenzja"),
    ("recenzent_2", "Recenzent", "Bartosz", "DemoRecenzja"),
    ("redaktor_1", "Redaktor", "Celina", "DemoRedakcja"),
    ("redaktor_2", "Redaktor", "Daniel", "DemoRedakcja"),
    ("korektor_1", "Korektor", "Eliza", "DemoKorekta"),
    ("korektor_2", "Korektor", "Filip", "DemoKorekta"),
    ("korektor_3", "Korektor", "Gabriela", "DemoKorekta"),
    ("korektor_4", "Korektor", "Hubert", "DemoKorekta"),
    ("weryfikator_1", "Weryfikator", "Iga", "DemoWeryfikacja"),
    ("weryfikator_2", "Weryfikator", "Jan", "DemoWeryfikacja"),
    ("weryfikator_3", "Weryfikator", "Karolina", "DemoWeryfikacja"),
    ("koordynator_1", "Koordynator", "Leon", "DemoKoordynacja"),
)


def case_match(model, field, value):
    return model.objects.alias(seed_value=Lower(field)).filter(
        seed_value=Lower(Value(value))
    )


def one_or_none(queryset, description):
    matches = list(queryset[:2])
    if len(matches) > 1:
        raise CommandError(f"Niejednoznaczny rekord: {description}. Nic nie zapisano.")
    return matches[0] if matches else None


class Command(BaseCommand):
    help = "Dodaje 10 fikcyjnych autorów (3 na czarnej liście) i 12 członków zespołu."

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-password", action="store_true",
            help="Zapytaj o wspólne hasło do nowo tworzonych kont demonstracyjnych.",
        )

    def handle(self, *args, **options):
        password = None
        if options["with_password"]:
            try:
                password = getpass("Hasło do nowych kont demo: ")
                confirmation = getpass("Powtórz hasło: ")
            except (EOFError, KeyboardInterrupt):
                raise CommandError("Przerwano przed zapisaniem danych.")
            if password != confirmation:
                raise CommandError("Hasła różnią się. Nic nie zapisano.")
            try:
                validate_password(password)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
        User = get_user_model()
        counts = {"autorzy": 0, "role": 0, "konta": 0, "osoby": 0}
        with transaction.atomic():
            for index, (first, last, pseudonym, blacklisted) in enumerate(AUTHORS, 1):
                email = f"demo.autor.{index:02d}@example.com"
                author = one_or_none(case_match(Author, "email", email), email)
                if author is None:
                    author = Author(
                        first_name=first, last_name=last, pseudonym=pseudonym,
                        email=email, is_blacklisted=blacklisted,
                        has_contract=index <= 4, contact=not blacklisted,
                    )
                    author.full_clean()
                    author.save()
                    counts["autorzy"] += 1
            roles = {}
            for name in sorted({entry[1] for entry in TEAM}):
                # Nazwa kanoniczna jest ważna dla sprawdzania ról przy kolacji CS.
                role = one_or_none(case_match(Role, "name", name), name)
                if role is not None and role.name != name:
                    raise CommandError(f"Rola ma inną wielkość liter: {role.name}. Ujednolić nazwę do {name}.")
                if role is None:
                    role = Role(name=name)
                    role.full_clean()
                    role.save()
                    counts["role"] += 1
                roles[name] = role
            for slug, role_name, first, last in TEAM:
                username = "demo_" + slug
                email = username + "@example.com"
                user = one_or_none(case_match(User, "username", username), username)
                person = one_or_none(case_match(Person, "email", email), email)
                if user is not None and (user.email != email or user.is_superuser or user.is_staff):
                    raise CommandError(f"Kolizja z istniejącym kontem {username}. Wycofano zapis.")
                if person is not None:
                    if user is None or person.user_id != user.pk:
                        raise CommandError(f"Kolizja profilu {email}. Wycofano zapis.")
                    # Zachowaj role, aktywność i inne ręczne zmiany istniejącej osoby.
                    continue
                if user is not None and Person.objects.filter(user=user).exists():
                    raise CommandError(f"Konto {username} ma już inny profil. Wycofano zapis.")
                if user is None:
                    user = User(username=username, email=email, first_name=first,
                                last_name=last, is_active=True, is_staff=False, is_superuser=False)
                    if password is None:
                        user.set_unusable_password()
                    else:
                        user.set_password(password)
                    user.full_clean()
                    user.save()
                    counts["konta"] += 1
                person = Person(
                    first_name=first, last_name=last, email=email, user=user,
                    is_active=True, is_coordinator=role_name == "Koordynator",
                )
                person.full_clean()
                person.save()
                person.roles.add(roles[role_name])
                counts["osoby"] += 1
        self.stdout.write(self.style.SUCCESS(
            "Dodano: " + ", ".join(f"{label}: {count}" for label, count in counts.items())
            + ". Istniejące rekordy i hasła pozostawiono bez zmian."
        ))
        for slug, role_name, _, _ in TEAM:
            self.stdout.write(f"demo_{slug}: {role_name}")
        if password is None:
            self.stdout.write("Nowe konta nie mają hasła. Ustaw je poleceniem changepassword LOGIN.")
