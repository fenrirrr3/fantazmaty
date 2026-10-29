"""Komenda zespołu: uzupełnianie standardowych ról."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from people.models import Role
from people.role_ordering import TEAM_ROLE_ORDER


def run(*args, **options):
    out = StringIO()
    call_command(*args, stdout=out, **options)
    return out.getvalue()


class EnsureTeamRolesCommandTests(TestCase):
    def test_adds_only_missing_standard_roles_and_is_repeatable(self):
        run('ensure_team_roles')
        Role.objects.filter(name__in=('Lektor', 'Ilustrator')).delete()
        Role.objects.filter(name='Recenzent').update(name='RECENZENT')
        expected = len(set(TEAM_ROLE_ORDER) | {'Ilustrator', 'Korektor audiobooków'})
        self.assertIn(f'Dodano ról: 2. Standardowy zestaw: {expected}.', run('ensure_team_roles'))
        self.assertTrue(Role.objects.filter(name='Lektor').exists())
        # Rola różniąca się tylko wielkością liter nie jest dublowana.
        self.assertEqual(Role.objects.filter(name__iexact='recenzent').count(), 1)
        self.assertIn('Dodano ról: 0.', run('ensure_team_roles'))
