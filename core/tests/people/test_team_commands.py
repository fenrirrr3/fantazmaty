"""Komendy zespołu: uzupełnianie standardowych ról i pomocniczy raport TSV członków zespołu."""
import csv
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from authors.models import Author
from fantazmaty.testing import create_member, create_superuser
from people.models import Person, Role
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


class ExportTeamMembersCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('export-member', 'Redaktor')
        person = cls.member.person_profile
        person.roles.add(Role.objects.get_or_create(name='Korektor')[0])
        person.dropbox_email = 'box@example.test'
        person.author_profile = Author.objects.create(first_name='A', last_name='B', email='author@example.test')
        person.save()
        cls.admin = create_superuser('export-admin')
        cls.admin_person = Person.objects.create(user=cls.admin, first_name='Ada', last_name='Admin',
                                                 email=cls.admin.email)
        cls.former = Person.objects.create(first_name='Była', last_name='Osoba', is_active=False)

    def rows(self, **options):
        return list(csv.reader(StringIO(run('export_team_members', **options)), delimiter='\t'))

    def test_one_row_per_role_with_ids_and_only_active_members_by_default(self):
        header, *rows = self.rows()
        self.assertEqual(header, ['Nazwisko i imię', 'E-mail', 'E-mail Dropbox', 'Funkcja', 'ID osoby', 'ID konta',
                                  'ID autora'])
        person = self.member.person_profile
        self.assertEqual(rows, [
            ['Admin Ada', self.admin.email, '', 'Superuser', str(self.admin_person.pk), str(self.admin.pk), ''],
            ['Testowy export-member', person.email, 'box@example.test', 'Korektor', str(person.pk),
             str(self.member.pk), str(person.author_profile_id)],
            ['Testowy export-member', person.email, 'box@example.test', 'Redaktor', str(person.pk),
             str(self.member.pk), str(person.author_profile_id)],
        ])

    def test_include_inactive_lists_former_members_without_role(self):
        rows = self.rows(include_inactive=True)
        self.assertIn(['Osoba Była', '', '', '', str(self.former.pk), '', ''], rows)
        self.assertEqual(len(rows), 5)
