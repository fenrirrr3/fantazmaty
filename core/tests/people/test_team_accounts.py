"""Profile zespołu i konta: jedno źródło ról i nazwisk, e-maile, osoby zewnętrzne, formularze osoby i konta w adminie."""
from datetime import datetime
from importlib import import_module
from types import SimpleNamespace

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.auth_backends import EmailBackend
from core.permissions import get_active_person_profile, has_role, is_coordinator
from fantazmaty.testing import create_member, create_superuser, form_value
from people.admin import PersonAdminForm
from people.models import Person, Role, format_local_datetime
from workflow.availability import eligible_role_users
from workflow.models import WorkflowRoleAssignment


class TeamAccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Redaktor')
        cls.coordinator = create_member('coord', 'Koordynator recenzji')

    def test_roles_and_names_have_one_profile_source(self):
        person = self.member.person_profile
        person.last_name = 'Nowe'
        person.save(update_fields=['last_name'])
        self.member.refresh_from_db()
        self.assertEqual(self.member.last_name, 'Nowe')
        self.member.groups.add(Group.objects.get_or_create(name='Korektor')[0])
        self.assertTrue(person.roles.filter(name='Korektor').exists())
        person.roles.remove(*person.roles.filter(name='Korektor'))
        self.assertFalse(self.member.groups.filter(name='Korektor').exists())
        self.coordinator.person_profile.roles.clear()
        self.assertFalse(is_coordinator(self.coordinator))

    def test_email_uniqueness_is_enforced_outside_forms(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user('duplicate', ' ADMIN@example.test ')
        get_user_model().objects.create_user('blank1', '')
        get_user_model().objects.create_user('blank2', '')

    def test_person_admin_form_email_rules(self):
        """Profil historyczny i zewnętrzny mogą nie mieć e-maila; aktywne konto zespołu musi go mieć."""
        historical = Person.objects.create(first_name='Dawna', last_name='Osoba', email=None)
        data = {'first_name': 'Dawna', 'last_name': 'Nowe nazwisko', 'email': '', 'is_active': 'on'}
        form = PersonAdminForm(data, instance=historical)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().email)
        person = self.member.person_profile
        form = PersonAdminForm({**data, 'user': self.member.pk}, instance=person)
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)
        form = PersonAdminForm(data={
            'first_name': person.first_name, 'last_name': person.last_name, 'user': self.member.pk,
            'is_active': 'on', 'is_external': 'on', 'roles': list(person.roles.values_list('pk', flat=True)),
            'email': ''}, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        person.refresh_from_db()
        self.assertTrue(person.is_external)
        self.assertFalse(person.is_active)

    def test_external_member_visible_but_cannot_login_or_take_work(self):
        person = self.member.person_profile
        person.is_external = True
        person.save()
        person.refresh_from_db()
        self.assertFalse(person.is_active)
        self.assertIsNone(get_active_person_profile(self.member))
        self.assertFalse(EmailBackend().user_can_authenticate(self.member))
        self.assertNotIn(self.member, eligible_role_users(WorkflowRoleAssignment.Role.EDITOR))
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:people_list'))
        self.assertContains(page, 'Zewnętrzny')
        self.assertIn(person, page.context['people'])

    def test_active_profile_without_account_is_not_hidden(self):
        person = Person.objects.create(first_name='Maria', last_name='BezKonta', email='no-account@example.com')
        self.assertTrue(Person.objects.active().filter(pk=person.pk).exists())
        self.assertTrue(person.can_show_team_contact)
        person.is_active = False
        person.save()
        self.assertFalse(person.can_show_team_contact)
        self.assertFalse(Person.objects.active().filter(pk=person.pk).exists())

    def test_account_admin_name_fields_fit_the_person_profile(self):
        from core.admin import AccountAdmin

        user = get_user_model().objects.create_user('acc12', 'acc12@example.com', 'test')
        request = RequestFactory().get('/')
        request.user = self.admin
        form = AccountAdmin(get_user_model(), site).get_form(request, user)(instance=user)
        self.assertEqual(form.fields['last_name'].max_length, Person._meta.get_field('last_name').max_length)

    def test_retiring_generic_role_preserves_access_and_revocation(self):
        """Migracja people.0015 usuwa rolę „Koordynator”, zachowując dostęp koordynatora i możliwość jego odebrania."""
        apps = MigrationExecutor(connection).loader.project_state([('people', '0014_contacts_and_team_status')]).apps
        HistoricalPerson, HistoricalRole = apps.get_model('people', 'Person'), apps.get_model('people', 'Role')
        profile = HistoricalPerson.objects.get(pk=self.member.person_profile.pk)
        profile.roles.add(HistoricalRole.objects.create(name='Koordynator'))
        import_module('people.migrations.0015_retire_generic_coordinator').retire(
            apps, SimpleNamespace(connection=connection))
        self.assertFalse(Role.objects.filter(name='Koordynator').exists())
        self.assertTrue(is_coordinator(self.member))
        self.assertTrue(has_role(self.member, 'Koordynator'))
        person = Person.objects.get(pk=profile.pk)
        person.save()
        self.assertFalse(Role.objects.filter(name='Koordynator').exists())
        self.assertIn(self.member, eligible_role_users(WorkflowRoleAssignment.Role.EDITING_COORDINATOR))
        person.is_coordinator = False
        person.save()
        self.assertFalse(is_coordinator(self.member))
        with self.assertRaises(ValidationError):
            Role(name='Koordynator').full_clean()


class PersonAdminPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin-ui')
        cls.member = get_user_model().objects.create_user('member-ui', email='member-ui@example.test')
        cls.member.date_joined = timezone.make_aware(datetime(2020, 6, 8, 12, 30))
        cls.member.last_login = timezone.make_aware(datetime(2026, 10, 1, 15, 45))
        cls.member.save()
        cls.person = Person.objects.create(user=cls.member, first_name='Ala', last_name='Osoba',
                                           email=cls.member.email, illustrator_preferences='Zachowaj')
        cls.role, _ = Role.objects.get_or_create(name='Ilustrator')
        cls.artist = Person.objects.create(first_name='Jan', last_name='Ilustrator',
                                           email='artist-ui@example.test', is_active=False)
        cls.artist.roles.add(cls.role)

    def setUp(self):
        self.client.force_login(self.admin)

    def person_page(self, person):
        return self.client.get(reverse('admin:people_person_change', args=[person.pk]))

    def test_contact_fields_are_separate_from_team_profiles_regardless_of_role(self):
        ordinary = self.person_page(self.person)
        self.assertEqual(ordinary.status_code, 200)
        for field in ('illustrator_portfolio', 'illustrator_preferences', 'illustrator_covers', 'illustrator_active'):
            self.assertNotContains(ordinary, f'name="{field}"')
            self.assertNotContains(self.person_page(self.artist), f'name="{field}"')
        self.person.roles.add(self.role)
        self.assertNotContains(self.person_page(self.person), 'name="illustrator_portfolio"')
        self.person.roles.remove(self.role)
        self.assertNotContains(self.person_page(self.person), 'name="illustrator_portfolio"')
        self.person.refresh_from_db()
        self.assertEqual(self.person.illustrator_preferences, 'Zachowaj')

    def test_dates_share_row_with_leave_and_have_empty_states(self):
        page = self.person_page(self.person)
        doc = html.fromstring(page.content.decode())
        halves = doc.xpath('//div[@class="person-admin-fieldsets"]/fieldset[contains(@class,"person-account-half")]')
        self.assertEqual(len(halves), 2)
        self.assertIn('Urlop', halves[0].text_content())
        self.assertIn('Aktywność konta', halves[1].text_content())
        self.assertContains(page, format_local_datetime(self.member.date_joined))
        self.assertContains(page, format_local_datetime(self.member.last_login))
        self.assertNotContains(page, 'name="account_date_joined"')
        self.member.last_login = None
        self.member.save(update_fields=['last_login'])
        self.assertContains(self.person_page(self.person), 'Jeszcze się nie logowano')
        self.assertContains(self.person_page(self.artist), 'Brak powiązanego konta')

    def test_non_illustrator_save_does_not_erase_hidden_illustrator_data(self):
        token = form_value(self.person_page(self.person))
        response = self.client.post(reverse('admin:people_person_change', args=[self.person.pk]), {
            'first_name': 'Alicja', 'last_name': 'Osoba', 'email': self.person.email,
            'is_active': 'on', '_save': 'Zapisz', '_edit_version': token,
        })
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db()
        self.assertEqual(self.person.first_name, 'Alicja')
        self.assertEqual(self.person.illustrator_preferences, 'Zachowaj')
