from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase

from authors.models import Author
from people.models import Person, Role
from core.permissions import has_role, is_coordinator
from core.management.commands.seed_demo import TEAM


MIGRATION_ROLES = {
    "Składacz",
    "Koordynator redakcji",
    "Koordynator audiobooków",
    "Koordynator weryfikacji",
    "Koordynator ilustracji",
    "Koordynator recenzji",
    "Koordynator korekty",
    "Koordynator rekrutacji",
}


class SeedDemoTests(TestCase):
    def seed(self, **options):
        call_command("seed_demo", stdout=StringIO(), **options)

    def test_creates_authors_blacklist_and_working_roles(self):
        self.seed()
        self.assertEqual(Author.objects.count(), 10)
        self.assertEqual(Author.objects.filter(is_blacklisted=True).count(), 3)
        self.assertEqual(Person.objects.count(), 12)
        self.assertEqual(
            set(Role.objects.values_list("name", flat=True)),
            {role for _, role, _, _ in TEAM} | MIGRATION_ROLES,
        )
        for slug, role, _, _ in TEAM:
            user = get_user_model().objects.get(username="demo_" + slug)
            self.assertTrue(has_role(user, role))
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.has_usable_password())
        self.assertTrue(is_coordinator(get_user_model().objects.get(username="demo_koordynator_1")))

    def test_repeat_preserves_existing_changes_and_password(self):
        self.seed()
        person = Person.objects.get(user__username="demo_recenzent_1")
        person.is_active = False
        person.save()
        person.user.set_password("Existing-password-674!")
        person.user.save()
        self.seed()
        person.refresh_from_db()
        self.assertFalse(person.is_active)
        self.assertTrue(person.user.check_password("Existing-password-674!"))
        self.assertEqual(Person.objects.count(), 12)
        self.assertEqual(Author.objects.count(), 10)

    def test_collision_rolls_back_all_new_records(self):
        get_user_model().objects.create_user(username="demo_recenzent_1", email="existing@example.com")
        with self.assertRaises(CommandError):
            self.seed()
        self.assertFalse(Author.objects.exists())
        self.assertEqual(
            set(Role.objects.values_list("name", flat=True)),
            MIGRATION_ROLES,
        )
        self.assertFalse(Person.objects.exists())
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_optional_password_is_set_on_new_accounts(self):
        with patch("core.management.commands.seed_demo.getpass", return_value="Demo-strong-8452!"):
            self.seed(with_password=True)
        user = get_user_model().objects.get(username="demo_recenzent_1")
        self.assertTrue(user.check_password("Demo-strong-8452!"))
