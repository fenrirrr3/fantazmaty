"""Role zespołu: profil osoby i grupy konta pozostają zgodne, a utrata roli koordynatorskiej odbiera uprawnienia."""
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
        self.assertEqual(group_names(user), {'Lektor'})
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ('Nowa', 'Osoba'))

    def test_profile_is_the_single_source_of_names_and_roles(self):
        self.person.last_name = 'Nowe'
        self.person.save(update_fields=['last_name'])
        self.member.refresh_from_db()
        self.assertEqual(self.member.last_name, 'Nowe')
        self.member.groups.add(Group.objects.get_or_create(name='Korektor')[0])
        self.assertIn('Korektor', role_names(self.person))
        self.person.roles.remove(*self.person.roles.filter(name='Korektor'))
        self.assertNotIn('Korektor', group_names(self.member))

    def test_role_side_changes_reach_account_groups(self):
        role = Role.objects.create(name='Grafik')
        role.people.add(self.person)
        self.assertIn('Grafik', group_names(self.member))
        role.people.clear()
        self.assertNotIn('Grafik', group_names(self.member))
        role.people.add(self.person)
        role.name = 'Grafik okładek'
        role.save()
        self.assertIn('Grafik okładek', group_names(self.member))
        role.delete()
        self.assertEqual(group_names(self.member), {'Redaktor'})

    def test_group_side_changes_reach_profile_roles(self):
        group = Group.objects.create(name='Tłumacz')
        group.user_set.add(self.member)
        self.assertIn('Tłumacz', role_names(self.person))
        group.user_set.clear()
        self.assertNotIn('Tłumacz', role_names(self.person))
        self.member.groups.add(group)
        group.delete()
        self.assertEqual(role_names(self.person), {'Redaktor'})

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

    def assert_revoked(self):
        person = Person.objects.get(user=self.coordinator)
        self.coordinator.refresh_from_db()
        self.assertFalse(person.is_coordinator)
        self.assertFalse(is_coordinator(self.coordinator))
        self.assertFalse(self.coordinator.is_staff)
        self.assertEqual(role_names(person), {'Recenzent'})
        self.assertEqual(group_names(self.coordinator), {'Recenzent'})

    def test_removing_person_from_coordinator_role_revokes_access(self):
        self.role.people.remove(self.coordinator.person_profile)
        self.assert_revoked()

    def test_renaming_coordinator_role_to_plain_role_revokes_access(self):
        self.role.name = 'Recenzje – archiwum'
        self.role.save()
        self.assert_revoked()

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
