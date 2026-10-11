"""Profile zespołu i konta: e-maile, osoby zewnętrzne, koordynacja przez role działowe, formularze osoby w adminie."""
from datetime import datetime

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
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
        person.save(update_fields=['is_external'])
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

    def test_coordinator_flag_follows_department_role_and_removal_revokes_access(self):
        """Koordynatora daje rola działowa; odebranie flagi zdejmuje rolę, a ogólnej roli „Koordynator” nie da się utworzyć."""
        person = self.coordinator.person_profile
        person.refresh_from_db()
        self.assertTrue(person.is_coordinator)
        self.assertTrue(has_role(self.coordinator, 'Koordynator'))
        self.assertIn(self.coordinator, eligible_role_users(WorkflowRoleAssignment.Role.EDITING_COORDINATOR))
        person.is_coordinator = False
        person.save()
        self.assertFalse(is_coordinator(self.coordinator))
        self.assertNotIn(self.coordinator, eligible_role_users(WorkflowRoleAssignment.Role.EDITING_COORDINATOR))
        self.assertFalse(person.roles.filter(name__startswith='Koordynator').exists())
        self.assertFalse(self.coordinator.groups.filter(name__startswith='Koordynator').exists())
        for name in ('Koordynator', ' koordynator zespołu '):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                Role(name=name).full_clean()
        Role(name='Koordynator recenzji 2').full_clean()


class PersonAdminPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin-ui')
        cls.member = get_user_model().objects.create_user('member-ui', email='member-ui@example.test')
        cls.member.date_joined = timezone.make_aware(datetime(2020, 6, 8, 12, 30))
        cls.member.last_login = timezone.make_aware(datetime(2026, 10, 1, 15, 45))
        cls.member.save()
        cls.person = Person.objects.create(user=cls.member, first_name='Ala', last_name='Osoba',
                                           email=cls.member.email, previous_data='Dawne nazwisko')
        cls.former = Person.objects.create(first_name='Jan', last_name='Dawny',
                                           email='former-ui@example.test', is_active=False)

    def setUp(self):
        self.client.force_login(self.admin)

    def person_page(self, person):
        return self.client.get(reverse('admin:people_person_change', args=[person.pk]))

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
        self.assertContains(self.person_page(self.former), 'Brak powiązanego konta')

    def test_admin_save_normalizes_names_and_links_existing_account(self):
        token = form_value(self.person_page(self.person))
        response = self.client.post(reverse('admin:people_person_change', args=[self.person.pk]), {
            'first_name': '  Alicja   Maria ', 'last_name': ' Osoba ', 'email': self.person.email,
            'dropbox_email': ' box@example.test ', 'previous_data': 'Dawne nazwisko',
            'is_active': 'on', '_save': 'Zapisz', '_edit_version': token,
        })
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((self.person.first_name, self.person.last_name), ('Alicja Maria', 'Osoba'))
        self.assertEqual(self.person.dropbox_email, 'box@example.test')
        self.assertEqual(self.person.previous_data, 'Dawne nazwisko')
        self.assertEqual(self.member.first_name, 'Alicja Maria')
        self.assertContains(self.person_page(self.person),
                            reverse('admin:auth_user_change', args=[self.member.pk]))

    def test_admin_form_rejects_duplicate_email_and_direct_coordinator_flag(self):
        data = {'first_name': 'Nowa', 'last_name': 'Osoba', 'is_active': 'on'}
        for email in ('FORMER-UI@example.test', ' member-ui@example.test '):
            with self.subTest(email=email):
                form = PersonAdminForm({**data, 'email': email})
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors['email'].as_data()[0].code, 'duplicate_email')
        form = PersonAdminForm({**data, 'email': 'nowa@example.test', 'is_coordinator': 'on'})
        self.assertFalse(form.is_valid())
        self.assertIn('konkretnego działu', str(form.non_field_errors()))
        account = get_user_model().objects.create_user('no-mail-ui', email='')
        form = PersonAdminForm({**data, 'user': account.pk})
        self.assertFalse(form.is_valid())
        self.assertIn('wymaga adresu e-mail', str(form.errors['email']))

    def test_clearing_coordinator_flag_in_admin_drops_coordinator_roles(self):
        coordinator = create_member('coord-ui', 'Koordynator korekty')
        person = coordinator.person_profile
        roles = Role.objects.filter(name__in=('Koordynator korekty', 'Korektor'))
        Role.objects.get_or_create(name='Korektor')
        form = PersonAdminForm({'first_name': person.first_name, 'last_name': person.last_name,
                                'email': person.email, 'is_active': 'on', 'user': coordinator.pk,
                                'roles': list(roles.values_list('pk', flat=True))}, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(set(person.roles.values_list('name', flat=True)), {'Korektor'})
        self.assertFalse(is_coordinator(coordinator))

    def test_revoke_action_is_superuser_only(self):
        from people.admin import PersonAdmin

        coordinator = create_member('coord-action', 'Koordynator recenzji')
        person = coordinator.person_profile
        request = RequestFactory().post('/')
        request.user = get_user_model().objects.create_user('staff-ui', is_staff=True)
        with self.assertRaises(PermissionDenied):
            PersonAdmin(Person, site).revoke_coordinator_access(request, Person.objects.filter(pk=person.pk))
        self.assertTrue(is_coordinator(coordinator))
        response = self.client.post(reverse('admin:people_person_changelist'), {
            'action': 'revoke_coordinator_access', '_selected_action': [person.pk], 'index': '0'}, follow=True)
        self.assertContains(response, 'Usunięto funkcje koordynatora')
        self.assertFalse(is_coordinator(coordinator))

    def test_changelist_and_role_pages(self):
        page = self.client.get(reverse('admin:people_person_changelist'))
        self.assertContains(page, 'Ala Osoba')
        self.assertContains(page, '<td class="field-display_roles">–</td>', html=True)
        role = Role.objects.get_or_create(name='Lektor')[0]
        role_page = self.client.get(reverse('admin:people_role_change', args=[role.pk]))
        self.assertNotContains(role_page, 'name="name"')
        self.assertContains(self.client.get(reverse('admin:people_role_add')), 'name="name"')
