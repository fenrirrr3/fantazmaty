"""Luki pokrycia obszaru ludzi: filtry listy zespołu, kilka urlopów naraz, odbieranie koordynacji i uprawnienia."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import serializers
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.permissions import can_manage_vacation, has_role
from core.selectors.people import team_availability
from core.selectors.post_layout import profile_post_layout_assignments
from core.services.vacations import cancel_planned_vacation, create_vacation
from fantazmaty.testing import create_member, create_superuser
from people.coordinator_access import revoke_coordinator
from people.leave_access import is_on_leave
from people.models import Person, Role, Vacation


class PeopleListFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('lista-redaktor', 'Redaktor')
        cls.proofreader = create_member('lista-korektor', 'Korektor')
        cls.both = create_member('lista-oboje', 'Redaktor')
        cls.both.person_profile.roles.add(Role.objects.get(name='Korektor'))
        cls.editor_role = Role.objects.get(name='Redaktor')
        cls.proofreader_role = Role.objects.get(name='Korektor')

    def setUp(self):
        self.client.force_login(self.editor)

    def names(self, params, status=200):
        response = self.client.get(reverse('core:people_list'), params)
        self.assertEqual(response.status_code, status)
        return response, sorted(person.first_name for person in response.context['people'])

    def test_role_filters_and_ignored_legacy_parameters(self):
        """Filtr ról: osoba z kilkoma rolami pojawia się raz; dawnych parametrów q i role już się nie obsługuje."""
        everyone = ['lista-korektor', 'lista-oboje', 'lista-redaktor']
        cases = {
            'fraza': ({'query': 'lista-korektor'}, ['lista-korektor']),
            'kilka ról': ({'roles': [self.editor_role.pk, self.proofreader_role.pk]}, everyone),
            'dawne q': ({'q': 'lista-korektor'}, everyone),
            'dawne role': ({'role': [str(self.proofreader_role.pk)]}, everyone),
        }
        for name, (params, expected) in cases.items():
            with self.subTest(name):
                response, names = self.names(params)
                self.assertEqual(names, expected)
        self.assertEqual(response.context['query'], '')

    def test_invalid_filter_returns_no_people_instead_of_everyone(self):
        for params in ({'roles': ['999999']}, {'query': 'x' * 300}):
            with self.subTest(params=list(params)):
                response, names = self.names(params, status=400)
                self.assertEqual(names, [])
                self.assertTrue(response.context['filter_form'].errors)


class VacationSelectionTests(TestCase):
    """Urlop przy osobie w panelu: trwający albo najbliższy zaplanowany, liczony z urlopów."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_member('urlopowicz', 'Redaktor')
        cls.person = cls.user.person_profile
        cls.today = timezone.localdate()

    def end(self, days):
        return timezone.now() + timedelta(days=days)

    def shown(self):
        from people.admin import PersonAdmin
        return PersonAdmin._leave(Person.objects.get(pk=self.person.pk))

    def test_nearest_of_several_planned_vacations_is_shown(self):
        later = create_vacation(user=self.user, person_id=self.person.pk, start_date=self.today + timedelta(days=20),
                                end_date=self.end(25))
        nearest = create_vacation(user=self.user, person_id=self.person.pk,
                                  start_date=self.today + timedelta(days=5), end_date=self.end(8))
        self.assertEqual(self.shown(), nearest)
        cancel_planned_vacation(user=self.user, vacation_id=nearest.pk)
        self.assertEqual(self.shown(), later)

    def test_overlapping_legacy_vacations_use_the_earliest_current_one(self):
        """Dawne dane z nakładającymi się trwającymi urlopami: przy osobie widać wcześniejszy z nich."""
        first = Vacation.objects.create(person=self.person, start_date=self.today - timedelta(days=5),
                                        until_revoked=True)
        Vacation.objects.create(person=self.person, start_date=self.today - timedelta(days=2), end_date=self.end(3))
        Vacation.objects.create(person=self.person, start_date=self.today - timedelta(days=9),
                                end_date=self.end(-1))
        self.assertEqual(self.shown(), first)
        self.assertTrue(is_on_leave(self.user))


class TeamAccessHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('pomocnik', 'Redaktor')
        cls.admin = create_superuser('pomocnik-admin')

    def test_role_and_vacation_checks_refuse_missing_input(self):
        cases = {
            'rola nie jest tekstem': has_role(self.member, None),
            'pusta nazwa roli': has_role(self.member, '   '),
            'brak urlopu': can_manage_vacation(self.admin, None),
            'konto bez zapisu w bazie': is_on_leave(get_user_model()(username='nowe')),
            'brak konta': is_on_leave(None),
        }
        for name, result in cases.items():
            with self.subTest(name):
                self.assertFalse(result)
        self.assertTrue(has_role(self.member, ' Redaktor '))

    def test_availability_and_post_layout_for_people_without_accounts(self):
        external = Person.objects.create(first_name='Zewnętrzna', last_name='Osoba', is_external=True)
        self.assertEqual(profile_post_layout_assignments(external), [])
        self.assertEqual(team_availability([None, get_user_model()(username='bez-pk')]), {})


class CoordinatorRevocationTests(TestCase):
    def test_revocation_for_profile_without_account_and_for_superuser(self):
        """Profil bez konta traci rolę; superużytkownik zachowuje dostęp do panelu administracyjnego."""
        coordinator_role = Role.objects.get_or_create(name='Koordynator redakcji')[0]
        external = Person.objects.create(first_name='Bez', last_name='Konta', is_external=True)
        external.roles.add(coordinator_role)
        admin = create_superuser('koordynator-admin')
        admin.is_staff = True
        admin.save()
        admin_person = Person.objects.create(user=admin, first_name='Admin', last_name='Koordynator',
                                             email=admin.email)
        admin_person.roles.add(coordinator_role)
        for person in (external, admin_person):
            with self.subTest(person=str(person)):
                revoke_coordinator(person)
                person.refresh_from_db()
                self.assertFalse(person.is_coordinator)
                self.assertFalse(person.roles.filter(pk=coordinator_role.pk).exists())
        admin.refresh_from_db()
        self.assertTrue(admin.is_staff)
        self.assertFalse(admin.groups.filter(name='Koordynator redakcji').exists())

    def test_fixture_restore_does_not_sync_account_with_profile(self):
        """Odtwarzanie z kopii (zapis surowy) nie nadpisuje danych konta ani jego grup."""
        user = get_user_model().objects.create_user('z-kopii', first_name='Konto')
        group = Group.objects.create(name='Grupa z kopii')
        user.groups.add(group)
        payload = json.dumps([{'model': 'people.person', 'pk': 9999,
                               'fields': {'first_name': 'Profil', 'last_name': 'Kopia', 'user': user.pk}}])
        for obj in serializers.deserialize('json', payload):
            obj.save()
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Konto')
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['Grupa z kopii'])
        self.assertTrue(Person.objects.filter(pk=9999, user=user).exists())
