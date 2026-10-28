"""Profile zespołu i konta: e-maile, osoby zewnętrzne, koordynacja przez role działowe, formularze osoby w adminie."""
from datetime import datetime

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.account_identity import validate_account_email
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

    def test_account_email_longer_than_field_is_refused(self):
        long_email = 'a' * 64 + '@' + ('b' * 63 + '.') * 3 + 'com'
        with self.assertRaisesMessage(ValidationError, 'Adres e-mail jest zbyt długi.'):
            validate_account_email(long_email)



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

    def test_revoke_action_removes_coordinator_access(self):
        """Akcja panelu (dostępnego tylko superuserowi) odbiera funkcje koordynatora."""
        coordinator = create_member('coord-action', 'Koordynator recenzji')
        person = coordinator.person_profile
        self.assertTrue(is_coordinator(coordinator))
        response = self.client.post(reverse('admin:people_person_changelist'), {
            'action': 'revoke_coordinator_access', '_selected_action': [person.pk], 'index': '0'}, follow=True)
        self.assertContains(response, 'Usunięto funkcje koordynatora')
        self.assertFalse(is_coordinator(coordinator))

    def test_mark_external_action(self):
        """Akcja zbiorcza „Oznacz jako zewnętrznych”: jak pole „Zewnętrzny” na ekranie osoby – osoba nieaktywna,
        konto bez logowania, poza Ostatnią aktywnością; osoby już zewnętrzne są pomijane i policzone w komunikacie."""
        member = create_member('external-action', 'Redaktor')
        person = member.person_profile
        outsider = Person.objects.create(first_name='Już', last_name='Zewnętrzny', is_external=True)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:people_person_changelist'), {
                'action': 'mark_external', '_selected_action': [person.pk, outsider.pk, self.former.pk], 'index': '0'},
                follow=True)
        self.assertContains(response, 'Oznaczono jako zewnętrznych: 2. Pominięto już zewnętrznych: 1.')
        for item in (person, outsider, self.former):
            with self.subTest(person=str(item)):
                item.refresh_from_db()
                self.assertEqual((item.is_external, item.is_active), (True, False))
        self.assertFalse(EmailBackend().user_can_authenticate(member))
        activity = self.client.get(reverse('core:last_activity'), {'days': 0, 'mode': 'login'})
        self.assertNotIn(person, activity.context['people'])
        changelist = html.fromstring(self.client.get(reverse('admin:people_person_changelist')).content)
        self.assertIn('mark_external', changelist.xpath('//select[@name="action"]/option/@value'))

    def test_changelist_and_role_pages(self):
        page = self.client.get(reverse('admin:people_person_changelist'))
        self.assertContains(page, 'Ala Osoba')
        self.assertContains(page, '<td class="field-display_roles">–</td>', html=True)
        role = Role.objects.get_or_create(name='Lektor')[0]
        role_page = self.client.get(reverse('admin:people_role_change', args=[role.pk]))
        self.assertNotContains(role_page, 'name="name"')
        self.assertContains(self.client.get(reverse('admin:people_role_add')), 'name="name"')


class PersonAccountTests(TestCase):
    """Ekran osoby jako jedyny punkt pracy z kontem: wspólny e-mail osoby i konta oraz blokada logowania."""
    PASSWORD = 'Bardzo-trudne-haslo-42'

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = create_superuser('konta-admin')
        cls.member = User.objects.create_user('ala@example.test', 'ala@example.test', cls.PASSWORD)
        cls.person = Person.objects.create(user=cls.member, first_name='Ala', last_name='Konto',
                                           email='ala@example.test', dropbox_email='ala-box@example.test')
        cls.taken = User.objects.create_user('inne-konto', 'zajety@example.test')
        cls.old_login = User.objects.create_user('stary-login@example.test', 'inny-adres@example.test')

    def form(self, instance=None, **values):
        data = {'first_name': 'Ola', 'last_name': 'Nowa', 'email': 'ola@example.test', 'is_active': 'on', **values}
        return PersonAdminForm({key: value for key, value in data.items() if value is not None}, instance=instance)

    def login(self, email):
        return EmailBackend().authenticate(None, username=email, password=self.PASSWORD)

    def test_account_creation_and_email_change_are_validated_in_person_form(self):
        """Konto z ekranu osoby: wymaga e-maila wolnego w kontach (także jako nazwa użytkownika) i mieszczącego się
        w nazwie użytkownika, osoby z ekipy,
        niezewnętrznej i bez konta; zmiana e-maila osoby z kontem też nie może zająć cudzego loginu."""
        cases = {
            'e-mail innego konta': ({'create_account': 'on', 'email': 'ZAJETY@example.test'}, None,
                                    'email', 'Konto z tym adresem e-mail już istnieje.'),
            'nazwa użytkownika innego konta': ({'create_account': 'on', 'email': 'stary-login@example.test'}, None,
                                               'email', 'Konto z tym adresem e-mail już istnieje.'),
            'zewnętrzny': ({'create_account': 'on', 'is_external': 'on'}, None,
                           'create_account', 'Zewnętrzny nie loguje się'),
            'spoza ekipy': ({'create_account': 'on', 'is_active': None}, None,
                            'create_account', '„wciąż w ekipie”'),
            'bez e-maila': ({'create_account': 'on', 'email': ''}, None, 'email', 'Konto wymaga adresu e-mail'),
            'wybrane konto': ({'create_account': 'on', 'user': 'taken'}, None, 'create_account', 'ma już konto'),
            'e-mail dłuższy niż login': ({'create_account': 'on', 'email': f'{"a" * 60}@{"b" * 60}.{"c" * 60}.pl'}, None,
                                         'email', 'Adres e-mail jest zbyt długi, by służyć jako login.'),
            'zmiana na cudzy login': ({'first_name': 'Ala', 'last_name': 'Konto', 'email': 'zajety@example.test',
                                       'user': 'member'}, 'person', 'email', 'Konto z tym adresem e-mail już istnieje.'),
        }
        for case, (values, instance, field, message) in cases.items():
            with self.subTest(case):
                if 'user' in values:
                    values['user'] = getattr(self, values['user']).pk
                form = self.form(getattr(self, instance) if instance else None, **values)
                self.assertFalse(form.is_valid())
                self.assertIn(message, ' '.join(form.errors.get(field, [])))
        self.assertTrue(self.form(create_account='on').is_valid())

    def test_email_is_shared_by_person_and_account_in_both_directions(self):
        """Zmiana e-maila osoby zmienia e-mail i nazwę użytkownika konta (logowanie nowym adresem), a zmiana na ekranie
        konta zmienia e-mail osoby; e-mail Dropboxa zostaje nietknięty."""
        form = self.form(self.person, first_name='Ala', last_name='Konto', email=' Ala.Nowa@Example.test ',
                         dropbox_email='ala-box@example.test', user=self.member.pk)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.member.refresh_from_db()
        self.assertEqual((self.member.email, self.member.username), ('ala.nowa@example.test',) * 2)
        self.assertEqual(self.login('ala.nowa@example.test'), self.member)
        self.assertIsNone(self.login('ala@example.test'))
        self.client.force_login(self.admin)
        url = reverse('admin:auth_user_change', args=[self.member.pk])
        response = self.client.post(url, {
            'first_name': 'Ala', 'last_name': 'Konto', 'email': 'Ala.Konto@example.test', 'is_superuser': '',
            'date_joined_0': '2026-01-01', 'date_joined_1': '10:00:00',
            '_edit_version': form_value(self.client.get(url)), '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((self.person.email, self.member.email, self.member.username), ('ala.konto@example.test',) * 3)
        self.assertEqual(self.person.dropbox_email, 'ala-box@example.test')
        page = self.client.post(url, {
            'first_name': 'Ala', 'last_name': 'Konto', 'email': 'zajety@example.test',
            'date_joined_0': '2026-01-01', 'date_joined_1': '10:00:00',
            '_edit_version': form_value(self.client.get(url)), '_save': 'Zapisz'})
        self.assertIn('Konto z tym adresem e-mail już istnieje.', str(page.context['adminform'].form.errors['email']))

    def test_save_outside_admin_cannot_take_another_accounts_login(self):
        """Zapis osoby poza formularzem z e-mailem innego konta jest odrzucany – osoba i konto zostają bez zmian."""
        self.person.email = 'zajety@example.test'
        with self.assertRaises(ValidationError), transaction.atomic():
            self.person.save()
        self.person.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((self.person.email, self.member.email), ('ala@example.test', 'ala@example.test'))

    def test_still_in_team_controls_login(self):
        """Odznaczenie „wciąż w ekipie” od razu blokuje logowanie, zaznaczenie je przywraca; zewnętrzny się nie loguje
        (także po zaznaczeniu „wciąż w ekipie”). E-mail Dropboxa zostaje bez zmian."""
        steps = [('odznaczenie', {'is_active': False}, False), ('zaznaczenie', {'is_active': True}, True),
                 ('zewnętrzny', {'is_external': True}, False),
                 ('zewnętrzny w ekipie', {'is_external': True, 'is_active': True}, False),
                 ('powrót z zewnętrznych', {'is_external': False, 'is_active': True}, True)]
        for step, changes, can_login in steps:
            for update_fields in (None, list(changes)):
                with self.subTest(step, update_fields=update_fields):
                    for name, value in changes.items():
                        setattr(self.person, name, value)
                    self.person.save(update_fields=update_fields)
                    self.member.refresh_from_db()
                    self.assertEqual(self.member.is_active, can_login)
                    self.assertEqual(self.login('ala@example.test') == self.member, can_login)
        self.person.refresh_from_db()
        self.assertEqual(self.person.dropbox_email, 'ala-box@example.test')

    def test_other_edits_do_not_unblock_and_inactive_link_blocks(self):
        """Edycja innych pól nie odblokowuje konta zablokowanego osobno; powiązanie konta z osobą spoza ekipy je blokuje."""
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.person.last_name = 'Zmienione'
        for update_fields in (['last_name'], None):
            with self.subTest(update_fields=update_fields):
                self.person.save(update_fields=update_fields)
                self.member.refresh_from_db()
                self.assertFalse(self.member.is_active)
        former = Person.objects.create(first_name='Jan', last_name='Dawny', email='inne-konto@example.test',
                                       is_active=False)
        former.user = self.taken
        former.save()
        self.taken.refresh_from_db()
        self.assertFalse(self.taken.is_active)
        self.assertEqual((self.taken.email, self.taken.username), ('inne-konto@example.test',) * 2)

    def test_person_without_existing_account_changes_nothing(self):
        """Osoba bez konta albo z kontem usuniętym w międzyczasie (nieaktualne user_id) – brak zmian konta."""
        from people.accounts import account_changes
        for label, user_id in (('bez konta', None), ('konto usunięte', 999999)):
            with self.subTest(label):
                person = Person(first_name='Ola', last_name='Nowa', email='ola@example.test', is_active=False,
                                user_id=user_id)
                self.assertEqual(account_changes(person, previous=None), {})

    def test_superuser_account_is_never_blocked_automatically(self):
        """Superuser z profilem osoby nie traci logowania po odznaczeniu „wciąż w ekipie” – administrator nie odetnie
        sobie dostępu; e-mail nadal podąża za osobą."""
        boss = get_user_model().objects.create_superuser('szef@example.test', 'szef@example.test', self.PASSWORD)
        person = Person.objects.create(user=boss, first_name='Szef', last_name='Admin', email='szef@example.test')
        for update_fields in (None, ['is_active']):
            with self.subTest(update_fields=update_fields):
                person.is_active = False
                person.save(update_fields=update_fields)
                boss.refresh_from_db()
                self.assertTrue(boss.is_active)
        person.email = 'szef.nowy@example.test'
        person.save()
        boss.refresh_from_db()
        self.assertEqual((boss.email, boss.username), ('szef.nowy@example.test',) * 2)
