import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.permissions import is_coordinator
from people.models import Person, Role


class TeamImportTests(TestCase):
    def write_data(self, body):
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".tsv",
            delete=False,
        )
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        with handle:
            handle.write(
                "Nazwisko i imię\tE-mail\tE-mail Dropbox\tFunkcja\n"
            )
            handle.write(body)
        return handle.name

    def test_preview_rolls_back_everything(self):
        roles_before = set(Role.objects.values_list("name", flat=True))
        path = self.write_data(
            "Nowak Anna\tanna@example.com\tdropbox@example.com\tRedaktor\n"
        )

        call_command("import_team_members", path)

        self.assertFalse(Person.objects.exists())
        self.assertFalse(get_user_model().objects.exists())
        self.assertSetEqual(
            set(Role.objects.values_list("name", flat=True)),
            roles_before,
        )

    def test_import_merges_roles_and_is_repeatable(self):
        path = self.write_data(
            "Nowak Anna\tanna@example.com\tdropbox@example.com\t"
            "Koordynator ilustracji\n"
            "Nowak Anna\tanna@example.com\tdropbox@example.com\tRedaktor\n"
            "Nowak Anna\tanna@example.com\tdropbox@example.com\tRedaktor\n"
        )

        call_command("import_team_members", path, commit=True)
        call_command("import_team_members", path, commit=True)

        person = Person.objects.get(email="anna@example.com")
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertSetEqual(
            set(person.roles.values_list("name", flat=True)),
            {"Koordynator ilustracji", "Redaktor"},
        )
        self.assertEqual(person.dropbox_email, "dropbox@example.com")
        self.assertTrue(person.is_coordinator)
        self.assertTrue(is_coordinator(person.user))

    def test_superuser_marker_elevates_user_without_creating_role(self):
        path = self.write_data(
            "Wiktorski Dawid\tdawid@example.com\tdawid@example.com\t"
            "Superuser\n"
            "Wiktorski Dawid\tdawid@example.com\tdawid@example.com\t"
            "Recenzent\n"
        )

        call_command("import_team_members", path, commit=True)

        user = get_user_model().objects.get(username="dawid@example.com")
        person = Person.objects.get(user=user)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertSetEqual(
            set(person.roles.values_list("name", flat=True)),
            {"Recenzent"},
        )
        self.assertFalse(Role.objects.filter(name="Superuser").exists())

    def test_existing_historical_person_is_reactivated_and_linked(self):
        person = Person.objects.create(
            first_name="Anna",
            last_name="Nowak",
            email="stary@example.com",
            is_active=False,
        )
        path = self.write_data(
            "Nowak Anna\tanna@example.com\tanna.dropbox@example.com\tKorektor\n"
        )

        call_command("import_team_members", path, commit=True)

        person.refresh_from_db()
        self.assertEqual(person.email, "anna@example.com")
        self.assertEqual(person.dropbox_email, "anna.dropbox@example.com")
        self.assertTrue(person.is_active)
        self.assertIsNotNone(person.user_id)
        self.assertIn("stary@example.com", person.previous_data)
