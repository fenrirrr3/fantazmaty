"""Role zespołu: profil osoby jest źródłem ról i imion (grupy konta nie są lustrem ról), a utrata roli koordynatorskiej odbiera uprawnienia."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from core.permissions import is_coordinator
from fantazmaty.testing import create_member
from people.models import Person, Role


def group_names(user):
    return set(user.groups.values_list('name', flat=True))


def role_names(person):
    return set(person.roles.values_list('name', flat=True))


class AccountGroupMirrorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('sync-member', 'Redaktor')
        cls.person = cls.member.person_profile

    def test_new_profile_takes_existing_account_groups_and_legacy_coordinator_access(self):
        user = get_user_model().objects.create_user('sync-new', 'sync-new@example.test')
        user.groups.add(Group.objects.create(name='Lektor'), Group.objects.create(name='Koordynator'))
        person = Person.objects.create(user=user, first_name='Nowa', last_name='Osoba', email=user.email)
        person.refresh_from_db()
        self.assertEqual(role_names(person), {'Lektor'})
        self.assertTrue(person.legacy_coordinator_access)
        self.assertTrue(person.is_coordinator)
        self.assertTrue(is_coordinator(user))
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ('Nowa', 'Osoba'))

    def test_profile_is_the_single_source_of_names_and_roles(self):
        """Nazwisko z osoby trafia do konta; grupy konta nie zmieniają ról osoby, a role nie zmieniają grup."""
        self.person.last_name = 'Nowe'
        self.person.save(update_fields=['last_name'])
        self.member.refresh_from_db()
        self.assertEqual(self.member.last_name, 'Nowe')
        before = group_names(self.member)
        self.member.groups.add(Group.objects.get_or_create(name='Korektor')[0])
        self.assertNotIn('Korektor', role_names(self.person))
        self.person.roles.add(Role.objects.get_or_create(name='Grafik')[0])
        self.assertEqual(group_names(self.member), before | {'Korektor'})

    def test_coordinator_flag_after_adding_role_requires_space_after_prefix(self):
        """Flaga koordynatora po dodaniu roli: „Koordynator <dział>” ją daje, nazwa bez spacji po „Koordynator” – nie (także w pamięci)."""
        for index, (name, expected) in enumerate((
            ('Koordynator redakcji', True),
            ('Koordynatorzy-stażyści', False),
            ('Koordynatorka', False),
        )):
            with self.subTest(name=name):
                user = create_member(f'flag-{index}', 'Redaktor')
                person = user.person_profile
                person.roles.add(Role.objects.get_or_create(name=name)[0])
                self.assertEqual(person.is_coordinator, expected)
                person.save()
                person.refresh_from_db()
                self.assertEqual(person.is_coordinator, expected)
                self.assertEqual(is_coordinator(user), expected)

    def test_account_save_keeps_profile_names_and_normalizes_email(self):
        self.member.first_name = 'Inne'
        self.member.email = '  Sync-Member@Example.COM '
        self.member.save()
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, self.person.first_name)
        self.assertEqual(self.member.email, 'sync-member@example.com')


class CoordinatorRevocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.coordinator = create_member('revoke-coord', 'Koordynator recenzji')
        cls.coordinator.person_profile.roles.add(Role.objects.get_or_create(name='Recenzent')[0])
        get_user_model().objects.filter(pk=cls.coordinator.pk).update(is_staff=True)
        cls.role = Role.objects.get(name='Koordynator recenzji')

    def assert_revoked(self, roles=('Recenzent',)):
        person = Person.objects.get(user=self.coordinator)
        self.coordinator.refresh_from_db()
        self.assertFalse(person.is_coordinator)
        self.assertFalse(is_coordinator(self.coordinator))
        self.assertFalse(self.coordinator.is_staff)
        self.assertEqual(role_names(person), set(roles))
        # Grupy koordynatorskie z dawnego lustra ról są sprzątane.
        self.assertFalse(self.coordinator.groups.filter(name__startswith='Koordynator').exists())

    def test_removing_person_from_coordinator_role_revokes_access(self):
        self.role.people.remove(self.coordinator.person_profile)
        self.assert_revoked()

    def test_renaming_coordinator_role_to_plain_role_revokes_access(self):
        self.role.name = 'Recenzje – archiwum'
        self.role.save()
        # Przemianowana rola zostaje przy osobie jako zwykła rola (bez lustra grup nic jej nie zdejmuje).
        self.assert_revoked(roles=('Recenzent', 'Recenzje – archiwum'))

    def test_deleting_coordinator_role_revokes_access(self):
        self.role.delete()
        self.assert_revoked()

    def test_another_department_role_keeps_coordinator_access(self):
        person = self.coordinator.person_profile
        person.roles.add(Role.objects.get_or_create(name='Koordynator korekty')[0])
        self.role.delete()
        self.assertTrue(is_coordinator(self.coordinator))
        self.coordinator.refresh_from_db()
        self.assertTrue(self.coordinator.is_staff)

    def test_renaming_plain_role_does_not_touch_coordinators(self):
        role = Role.objects.get(name='Recenzent')
        role.name = 'Recenzent tekstów'
        role.save()
        self.assertTrue(is_coordinator(self.coordinator))
