"""Nazwy ról zespołu: jeden moduł people.roles, spójny z komendą ensure_team_roles i bez rozproszonych literałów."""
import importlib
import inspect
import re
from io import StringIO
from types import SimpleNamespace
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

from core import permissions
from fantazmaty.testing import create_member
from people import roles
from people.models import Person, Role

ROLE_CONSTANTS = {
    name: value for name, value in vars(roles).items()
    if name.isupper() and not name.startswith('_') and isinstance(value, str)
}

# Świadome wyjątki: etykiety interfejsu, które brzmią jak nazwa roli, ale niczego nie filtrują.
UI_LABELS = {
    'core/audiobook_forms.py': {'Lektor'},  # etykieta pola formularza
    'core/recruitment_roles.py': {'Lektor'},  # fraza tematu zgłoszenia rekrutacyjnego
    'core/supervision.py': {'Recenzent', 'Ilustrator', 'Redaktor', 'Korektor', 'Weryfikator'},  # etykiety stopki
    'core/table_sorting.py': {'Tłumacz', 'Redaktor', 'Korektor', 'Lektor', 'Dźwiękowiec', 'Recenzent', 'Weryfikator'},  # nagłówki kolumn
    'core/templates/core/editor_activity.html': {'Redaktor'},  # nagłówek kolumny raportu
    'core/templates/core/proofreader_activity.html': {'Korektor'},
    'core/templates/core/verifier_activity.html': {'Weryfikator'},
    'texts/admin.py': {'Tłumacz'},  # nagłówek kolumny w adminie
    'workflow/models.py': {'Redaktor'},  # etykieta roli workflow (WorkflowRoleAssignment.Role)
    'core/selectors/reports.py': {'Recenzent', 'Korektor', 'Weryfikator', 'Redaktor'},  # nagłówki kolumn raportów
    'core/selectors/texts.py': {'Redaktor'},  # nagłówek kolumny
    'core/activity.py': {'Social Media'},  # nazwa zakładki w dzienniku aktywności (jak etykieta menu)
}

SKIPPED_DIRS = {'migrations', 'tests', 'staticfiles', 'venv', 'node_modules', '__pycache__'}


def role_literals(text):
    """Pary (linia, nazwa) dla literałów napisów równych nazwie roli; komentarze są pomijane."""
    names = sorted(set(roles.STANDARD_ROLES + roles.RETIRED_ROLES), key=len, reverse=True)
    exact = re.compile(r'''(["'])(%s) ?\1''' % '|'.join(map(re.escape, names)))
    # Ręczne rozpoznawanie koordynatora po nazwie zapisanej małymi literami.
    folded = re.compile(r'''(?:==|!=|startswith\(|\bin \(?)\s*(["'])(koordynator) ?\1''')
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('#'):
            continue
        for pattern in (exact, folded):
            for match in pattern.finditer(line):
                yield number, match.group(2)


class RoleConstantsTests(SimpleTestCase):
    def test_every_constant_is_standard_or_retired(self):
        """Każda stała nazwy roli należy do standardowego zestawu albo do ról wycofanych."""
        known = set(roles.STANDARD_ROLES) | set(roles.RETIRED_ROLES)
        for name, value in ROLE_CONSTANTS.items():
            with self.subTest(constant=name):
                self.assertIn(value, known)

    def test_groups_are_subsets_of_standard_roles(self):
        """Kolejność wyświetlania i grupy ról zawierają tylko role standardowe, a koordynatorzy działów są rozpoznawani."""
        for group in ('TEAM_ROLE_ORDER', 'COORDINATOR_ROLES', 'AUDIOBOOK_ROLES'):
            with self.subTest(group=group):
                self.assertLessEqual(set(getattr(roles, group)), set(roles.STANDARD_ROLES))
        self.assertEqual(
            {name for name in roles.STANDARD_ROLES if roles.is_coordinator_role_name(name)},
            set(roles.COORDINATOR_ROLES),
        )

    def test_is_coordinator_role_name(self):
        """Koordynatorem jest „Koordynator” i „Koordynator <dział>” (bez względu na wielkość liter), nic innego."""
        for name, expected in (
            ('Koordynator', True), ('koordynator', True), ('KOORDYNATOR REDAKCJI', True),
            ('Koordynator korekty', True), ('Koordynatorzy-stażyści', False), ('Koordynatorka', False),
            ('Redaktor', False), ('Prawa ręka koordynatora', False), ('', False), (None, False),
        ):
            with self.subTest(name=name):
                self.assertIs(roles.is_coordinator_role_name(name), expected)

    def test_no_role_name_literals_outside_roles_module(self):
        """Kod produkcyjny (.py, .html, .js) nie zapisuje nazw ról literalnie – poza świadomymi wyjątkami."""
        root = Path(settings.BASE_DIR)
        found = []
        for path in sorted(root.rglob('*')):
            if path.suffix not in ('.py', '.html', '.js') or SKIPPED_DIRS.intersection(path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            if rel == 'people/roles.py':
                continue
            allowed = UI_LABELS.get(rel, set())
            found += [f'{rel}:{number}: {name}' for number, name in role_literals(path.read_text(encoding='utf-8'))
                      if name not in allowed]
        self.assertEqual(found, [], 'Użyj stałych z people/roles.py.')


class RoleQueryTests(TestCase):
    def test_coordinator_role_q_matches_name_check(self):
        """coordinator_role_q wybiera z bazy dokładnie te role, które rozpoznaje is_coordinator_role_name."""
        for name in ('KOORDYNATOR TESTÓW', 'Koordynatorzy-stażyści', 'Koordynatorka', 'Redaktor'):
            Role.objects.get_or_create(name=name)
        names = Role.objects.values_list('name', flat=True)
        self.assertEqual(
            set(Role.objects.filter(roles.coordinator_role_q()).values_list('name', flat=True)),
            {name for name in names if roles.is_coordinator_role_name(name)},
        )
        self.assertIn('KOORDYNATOR TESTÓW', names)

    def test_ensure_team_roles_creates_standard_roles(self):
        """Komenda ensure_team_roles tworzy wszystkie role standardowego zestawu z people/roles.py."""
        Role.objects.all().delete()
        call_command('ensure_team_roles', stdout=StringIO())
        self.assertEqual(set(Role.objects.values_list('name', flat=True)), set(roles.STANDARD_ROLES))


class HonoraryRoleTests(TestCase):
    def test_emperor_role_exists_and_grants_nothing(self):
        """„Imperator Ludzkości” powstaje przy migrate, jest pierwsza w kolejności ról i nie jest rolą koordynatora;
        osoba z samą tą rolą ma dokładnie te uprawnienia co członek zespołu bez ról (każda funkcja can_/is_
        z core.permissions przyjmująca samo konto)."""
        self.assertTrue(Role.objects.filter(name=roles.EMPEROR).exists())
        self.assertEqual(roles.TEAM_ROLE_ORDER[0], roles.EMPEROR)
        self.assertFalse(roles.is_coordinator_role_name(roles.EMPEROR))
        self.assertFalse(Role.objects.filter(roles.coordinator_role_q(), name=roles.EMPEROR).exists())
        emperor, plain = create_member('emperor', roles.EMPEROR), create_member('plain', roles.EDITOR)
        plain.person_profile.roles.clear()
        checks = [(name, function) for name, function in vars(permissions).items()
                  if name.startswith(('can_', 'is_', 'coordinates_')) and inspect.isfunction(function)
                  and list(inspect.signature(function).parameters) == ['user']]
        self.assertGreater(len(checks), 10)
        for name, function in checks:
            with self.subTest(check=name):
                self.assertEqual(function(emperor), function(plain))
        self.assertTrue(permissions.has_role(emperor, roles.EMPEROR))
        self.assertFalse(permissions.can_manage_social_media(emperor))


class EmperorRoleMigrationTests(TestCase):
    migration = importlib.import_module('people.migrations.0019_add_emperor_role')

    def run_migration(self, function):
        getattr(self.migration, function)(django_apps, SimpleNamespace(connection=connection))

    def holders(self):
        return list(Person.objects.filter(roles__name=self.migration.ROLE).values_list('last_name', flat=True))

    def test_role_goes_to_person_by_profile_or_account_email(self):
        """Rolę dostaje osoba z e-mailem profilu (bez wielkości liter), a gdy takiej nie ma – osoba, której konto
        ma ten e-mail; bez pasującej osoby powstaje tylko rola. Cofnięcie usuwa przypisanie i rolę."""
        account = get_user_model().objects.create_user('emperor-account')
        cases = (
            ('brak osoby', [], []),
            ('e-mail konta', [dict(last_name='Konto', email='inny@example.com', user=account)], ['Konto']),
            ('e-mail profilu', [dict(last_name='Konto', email='inny@example.com', user=account),
                                dict(last_name='Profil', email='DAWIDWIKTORSKI@gmail.com')], ['Profil']),
        )
        for label, people, expected in cases:
            with self.subTest(label):
                Person.objects.all().delete()
                self.run_migration('remove_emperor_role')
                self.assertFalse(Role.objects.filter(name=self.migration.ROLE).exists())
                for data in people:
                    Person.objects.create(first_name='Osoba', **data)
                # Konto zwykle podąża za e-mailem osoby; starsze konta mogą mieć inny – stąd update() z pominięciem sygnałów.
                get_user_model().objects.filter(pk=account.pk).update(email='DawidWiktorski@Gmail.com')
                self.run_migration('add_emperor_role')
                self.run_migration('add_emperor_role')  # ponowne uruchomienie niczego nie dubluje
                self.assertEqual(Role.objects.filter(name__iexact=self.migration.ROLE).count(), 1)
                self.assertEqual(self.holders(), expected)
                self.run_migration('remove_emperor_role')
                self.assertEqual((Role.objects.filter(name=self.migration.ROLE).exists(), self.holders()), (False, []))
                self.assertEqual(Person.objects.count(), len(people))


class EmperorRoleRenameMigrationTests(TestCase):
    migration = importlib.import_module('people.migrations.0020_rename_emperor_role')

    def run_migration(self, function):
        getattr(self.migration, function)(django_apps, SimpleNamespace(connection=connection))

    def test_rename_keeps_holders_and_merges_existing_role(self):
        """Zmiana nazwy „Bóg-Imperator” → „Imperator Ludzkości” zachowuje przypisania i da się cofnąć; gdy rola
        o nowej nazwie już istnieje, przypisania przechodzą do niej, a stara rola znika; brak starej roli – bez zmian."""
        old, new = self.migration.OLD, self.migration.NEW
        cases = (('zwykła zmiana nazwy', False), ('nowa nazwa już istnieje', True))
        for label, new_exists in cases:
            with self.subTest(label):
                Role.objects.filter(name__in=[old, new]).delete()
                person = Person.objects.create(first_name='Osoba', last_name=label)
                person.roles.add(Role.objects.create(name=old))
                if new_exists:
                    Role.objects.create(name=new)
                self.run_migration('forwards')
                self.assertFalse(Role.objects.filter(name=old).exists())
                self.assertEqual(list(person.roles.values_list('name', flat=True)), [new])
                self.run_migration('backwards')
                self.assertEqual(list(person.roles.values_list('name', flat=True)), [old])
                self.assertFalse(Role.objects.filter(name=new).exists())
        Role.objects.filter(name__in=[old, new]).delete()
        self.run_migration('forwards')
        self.assertFalse(Role.objects.filter(name__in=[old, new]).exists())
