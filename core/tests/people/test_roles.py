"""Nazwy ról zespołu: jeden moduł people.roles, spójny z komendą ensure_team_roles i bez rozproszonych literałów."""
import re
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from people import roles
from people.models import Role

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
