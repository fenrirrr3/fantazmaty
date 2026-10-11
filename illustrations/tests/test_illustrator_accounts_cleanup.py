"""Ilustratorzy z kontami: osoby z rolą Ilustrator i innymi rolami zachowują dostęp do Ilustracji,
a migracja people 0017 usuwa wyłącznie niezalogowane konta samych ilustratorów obecnych w spisie.

Migracja nie zmienia schematu, więc test wywołuje jej funkcję na bieżących modelach.
"""
import contextlib
import io
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.permissions import can_view_illustrations
from fantazmaty.testing import create_member
from illustrations.models import Illustrator
from people.models import Person, Role, Vacation

forward = import_module('people.migrations.0017_remove_illustrator_only_accounts').forward


class IllustratorAccessTests(TestCase):
    def test_illustrator_with_another_role_keeps_illustrations_access(self):
        user = create_member('ilustratorka', 'Redaktor')
        user.person_profile.roles.add(Role.objects.get_or_create(name='Ilustrator')[0])
        self.assertTrue(can_view_illustrations(user))
        self.client.force_login(user)
        response = self.client.get(reverse('core:home'))
        self.assertContains(response, reverse('illustrations:illustration_list'))
        self.assertEqual(self.client.get(reverse('illustrations:illustration_list')).status_code, 200)

    def test_member_without_illustrator_role_has_no_access(self):
        user = create_member('redaktor', 'Redaktor')
        self.assertFalse(can_view_illustrations(user))
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('illustrations:illustration_list')).status_code, 403)


class IllustratorOnlyAccountCleanupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Ilustrator')

    def person(self, username, *, roles=('Ilustrator',), logged_in=False, account=True, first=None, last='Rysownik'):
        user = None
        if account:
            user = get_user_model().objects.create_user(username=username, email=f'{username}@example.test')
            if logged_in:
                get_user_model().objects.filter(pk=user.pk).update(last_login=timezone.now())
        person = Person.objects.create(first_name=first or username, last_name=last,
                                       email=f'{username}@example.test' if account else None, user=user)
        person.roles.add(*(Role.objects.get_or_create(name=name)[0] for name in roles))
        return person

    def run_migration(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            forward(apps, SimpleNamespace(connection=connection))
        return output.getvalue()

    def exists(self, person):
        return Person.objects.filter(pk=person.pk).exists()

    def test_removes_only_never_logged_illustrators_present_in_directory(self):
        removed = self.person('anna')
        Illustrator.objects.create(first_name='Inna', last_name='Osoba', email='ANNA@example.test')
        no_account = self.person('bez-konta', account=False, first='Basia')
        Illustrator.objects.create(first_name='Basia', last_name='Rysownik')
        logged = self.person('celina', logged_in=True)
        Illustrator.objects.create(first_name='celina', last_name='Rysownik')
        other_role = self.person('dorota', roles=('Ilustrator', 'Redaktor'))
        Illustrator.objects.create(first_name='dorota', last_name='Rysownik')
        output = self.run_migration()
        self.assertFalse(self.exists(removed))
        self.assertFalse(get_user_model().objects.filter(username='anna').exists())
        self.assertFalse(self.exists(no_account))
        self.assertTrue(self.exists(logged))
        self.assertTrue(self.exists(other_role))
        self.assertTrue(get_user_model().objects.filter(username='dorota').exists())
        self.assertIn('Usunięte konta samych ilustratorów (2)', output)
        self.assertEqual(Illustrator.objects.count(), 4)

    def test_keeps_people_without_unambiguous_directory_entry_or_with_related_data(self):
        missing = self.person('ewa')
        ambiguous = self.person('fela', account=False, first='Fela')
        Illustrator.objects.create(first_name='Fela', last_name='Rysownik')
        Illustrator.objects.create(first_name='Inna', last_name='Kreska', pseudonym='Fela Rysownik')
        busy = self.person('gosia')
        Illustrator.objects.create(first_name='gosia', last_name='Rysownik', email='gosia@example.test')
        today = timezone.localdate()
        Vacation.objects.create(person=busy, start_date=today, end_date=today)
        admin = self.person('hania')
        get_user_model().objects.filter(username='hania').update(is_staff=True)
        Illustrator.objects.create(first_name='hania', last_name='Rysownik')
        output = self.run_migration()
        for person in (missing, ambiguous, busy, admin):
            self.assertTrue(self.exists(person), person)
        self.assertIn('ewa Rysownik (brak jednoznacznego wpisu w spisie ilustratorów)', output)
        self.assertIn('Fela Rysownik (brak jednoznacznego wpisu', output)
        self.assertIn('gosia Rysownik (powiązane dane:', output)
        self.assertNotIn('hania', output)
        self.assertIn('Usunięte konta samych ilustratorów (0): brak', output)
