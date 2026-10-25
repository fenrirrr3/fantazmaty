"""Usługa urlopów: uprawnienia, reguły dat i nakładania, zakończenie i odwołanie, zbiorcza dostępność zespołu."""
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.permissions import can_manage_vacation, has_role
from core.selectors.people import team_availability
from core.selectors.post_layout import profile_post_layout_assignments
from core.services.vacations import cancel_planned_vacation, create_vacation, finish_vacation, update_vacation
from fantazmaty.testing import create_member, create_superuser
from people.leave_access import is_on_leave, require_available
from people.models import Person, Role, Vacation


class VacationServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.member_user = User.objects.create_user('member', 'member@example.test', 'x')
        cls.member = Person.objects.create(user=cls.member_user, first_name='Jan', last_name='Test',
                                           email=cls.member_user.email)
        cls.other_user = User.objects.create_user('other', 'other@example.test', 'x')
        cls.other = Person.objects.create(user=cls.other_user, first_name='Anna', last_name='Test',
                                          email=cls.other_user.email)
        cls.coordinator_user = User.objects.create_user('coordinator', 'coordinator@example.test', 'x')
        coordinator = Person.objects.create(user=cls.coordinator_user, first_name='Ewa', last_name='Test',
                                            email=cls.coordinator_user.email)
        coordinator.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        cls.today = timezone.localdate()

    def create(self, user=None, person=None, **data):
        data.setdefault('start_date', self.today)
        return create_vacation(user=user or self.member_user, person_id=(person or self.member).pk, **data)

    def test_member_creates_own_open_ended_vacation_and_is_on_leave(self):
        vacation = self.create(until_revoked=True)
        self.assertTrue(vacation.until_revoked)
        self.assertIsNone(vacation.end_date)
        self.assertTrue(is_on_leave(self.member_user))
        with self.assertRaises(ValidationError):
            require_available(self.member_user)

    def test_member_cannot_manage_someone_else(self):
        with self.assertRaises(PermissionDenied):
            self.create(person=self.other, until_revoked=True)
        self.assertFalse(Vacation.objects.exists())

    def test_coordinator_manages_any_team_member(self):
        vacation = self.create(user=self.coordinator_user, person=self.other, until_revoked=True)
        self.assertEqual(vacation.person, self.other)

    def test_inactive_person_cannot_receive_vacation(self):
        Person.objects.filter(pk=self.other.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.create(user=self.coordinator_user, person=self.other, until_revoked=True)

    def test_start_date_in_the_past_or_too_far_ahead_is_rejected(self):
        for start in (self.today - timedelta(days=1), self.today + timedelta(days=400)):
            with self.subTest(start=start), self.assertRaises(ValidationError):
                self.create(start_date=start, until_revoked=True)

    def test_midnight_vacation_boundary_does_not_overlap(self):
        """Chroni przed uznaniem urlopu zaczynającego się o północy za nakładający się na poprzedni."""
        midnight = timezone.make_aware(datetime.combine(self.today + timedelta(days=2), time.min))
        Vacation.objects.create(person=self.member, start_date=self.today, end_date=midnight)
        following = Vacation(person=self.member, start_date=self.today + timedelta(days=2),
                             end_date=midnight + timedelta(days=1))
        following.full_clean()
        following.save()
        self.assertEqual(list(Vacation.objects.filter(person=self.member).order_by('start_date')
                              .values_list('start_date', flat=True)), [self.today, self.today + timedelta(days=2)])
        # Granica jest dokładnie o północy: urlop od dnia wcześniej już się nakłada.
        with self.assertRaises(ValidationError):
            Vacation(person=self.member, start_date=self.today + timedelta(days=1),
                     end_date=midnight + timedelta(days=1)).full_clean()

    def test_planned_vacation_does_not_block_work_yet(self):
        from core.selectors.people import user_leave_information
        self.create(start_date=self.today + timedelta(days=5), until_revoked=True)
        self.assertFalse(is_on_leave(self.member_user))
        leave = user_leave_information(self.member_user)
        self.assertEqual((leave['vacation']['start_date'], leave['is_active']), (self.today + timedelta(days=5), False))

    def test_current_vacation_takes_precedence_over_planned_one(self):
        from core.selectors.people import user_leave_information
        self.create(start_date=self.today + timedelta(days=10), until_revoked=True)
        self.create(start_date=self.today, end_date=self.today + timedelta(days=2))
        leave = user_leave_information(self.member_user)
        self.assertEqual((leave['vacation']['start_date'], leave['vacation']['until_revoked'], leave['is_active']),
                         (self.today, False, True))

    def test_finish_ends_an_active_vacation_now(self):
        vacation = self.create(until_revoked=True)
        finished = finish_vacation(user=self.member_user, vacation_id=vacation.pk)
        self.assertFalse(finished.until_revoked)
        self.assertLessEqual(finished.end_date, timezone.now())
        self.assertFalse(is_on_leave(self.member_user))

    def test_finish_rejects_planned_vacation(self):
        vacation = self.create(start_date=self.today + timedelta(days=3), until_revoked=True)
        with self.assertRaises(ValidationError):
            finish_vacation(user=self.member_user, vacation_id=vacation.pk)

    def test_cancel_only_planned_vacation(self):
        planned = self.create(start_date=self.today + timedelta(days=3), until_revoked=True)
        cancel_planned_vacation(user=self.member_user, vacation_id=planned.pk)
        self.assertFalse(Vacation.objects.filter(pk=planned.pk).exists())
        current = self.create(until_revoked=True)
        with self.assertRaises(ValidationError):
            cancel_planned_vacation(user=self.member_user, vacation_id=current.pk)

    def test_other_member_cannot_cancel_or_finish(self):
        vacation = self.create(until_revoked=True)
        for operation in (finish_vacation, cancel_planned_vacation):
            with self.subTest(operation=operation.__name__), self.assertRaises(PermissionDenied):
                operation(user=self.other_user, vacation_id=vacation.pk)

    def test_finished_vacation_cannot_be_edited(self):
        vacation = self.create(until_revoked=True)
        finish_vacation(user=self.member_user, vacation_id=vacation.pk)
        later = timezone.now() + timedelta(minutes=1)
        with patch('core.services.vacations.timezone.now', return_value=later), self.assertRaises(ValidationError):
            update_vacation(user=self.member_user, vacation_id=vacation.pk, start_date=self.today,
                            until_revoked=True)

    def test_update_changes_planned_dates(self):
        vacation = self.create(start_date=self.today + timedelta(days=3), until_revoked=True)
        updated = update_vacation(user=self.member_user, vacation_id=vacation.pk,
                                  start_date=self.today + timedelta(days=4), end_date=self.today + timedelta(days=6))
        self.assertEqual(updated.start_date, self.today + timedelta(days=4))
        self.assertFalse(updated.until_revoked)
        self.assertEqual(timezone.localtime(updated.end_date).date(), self.today + timedelta(days=6))

    def test_until_revoked_flag_must_be_boolean(self):
        with self.assertRaises(ValidationError):
            self.create(until_revoked='yes')

    def test_team_availability_batch_matches_single_lookups(self):
        from core.permissions import get_active_person_profile
        from core.selectors.people import team_availability, user_leave_information

        User = get_user_model()
        admin = create_superuser()
        Vacation.objects.create(person=self.other, start_date=self.today - timedelta(days=1),
                                end_date=timezone.now() + timedelta(days=3))
        Vacation.objects.create(person=self.member, start_date=self.today + timedelta(days=10),
                                end_date=timezone.now() + timedelta(days=12))
        inactive = User.objects.create_user('inactive', 'inactive@example.com', 'x', is_active=False)
        Person.objects.create(user=inactive, first_name='Nie', last_name='Aktywny', email=inactive.email)
        hidden = User.objects.create_user('hidden', 'hidden@example.com', 'x')
        Person.objects.create(user=hidden, first_name='Ukryty', last_name='Profil', email=hidden.email,
                              is_active=False)
        Vacation.objects.create(person=hidden.person_profile, start_date=self.today, until_revoked=True)
        users = [admin, self.coordinator_user, self.other_user, self.member_user, inactive, hidden]
        batch = team_availability(users)
        for user in users:
            with self.subTest(user=user.username):
                self.assertEqual(batch[user.pk]['profile'], get_active_person_profile(user))
                self.assertEqual(batch[user.pk]['on_leave'], is_on_leave(user))
                self.assertEqual(batch[user.pk]['leave_information'], user_leave_information(user))


    def test_unavailable_reason_without_prepared_cache(self):
        """Powód niedostępności liczony bez gotowej pamięci zespołu: nieaktywne konto, urlop (o ile się liczy), nic."""
        from core.selectors.texts import unavailable_reason
        Vacation.objects.create(person=self.other, start_date=self.today, until_revoked=True)
        inactive = get_user_model().objects.create_user('nieaktywny', 'nieaktywny@example.test', 'x', is_active=False)
        cases = ((self.member_user, True, ''), (self.other_user, True, 'jest na urlopie'),
                 (self.other_user, False, ''), (inactive, True, 'ma nieaktywne konto'), (None, True, ''))
        cache = {}
        for user, leave, reason in cases:
            with self.subTest(user=user and user.username, leave=leave):
                self.assertEqual(unavailable_reason(user, leave=leave, cache=cache), reason)
        self.assertEqual(cache[(self.other_user.pk, True)], 'jest na urlopie')
        self.assertEqual(unavailable_reason(inactive, leave=False), 'ma nieaktywne konto')
        with patch('people.leave_access.is_on_leave', side_effect=AssertionError('bez zapytania')):
            self.assertEqual(unavailable_reason(self.other_user, cache=cache), 'jest na urlopie')


class LeaveFieldRuleTests(SimpleTestCase):
    """Reguły dat urlopu w modelu (bez bazy): wpis urlopu, jego opis i stan."""

    def assert_errors(self, obj, expected):
        with self.assertRaises(ValidationError) as caught:
            obj.clean()
        self.assertEqual(set(caught.exception.message_dict), expected)

    def test_vacation_entry_rules_and_description(self):
        today = timezone.localdate()
        end = timezone.make_aware(datetime(today.year, today.month, today.day, 15, 30)) + timedelta(days=2)
        self.assert_errors(Vacation(until_revoked=True), {'start_date'})
        self.assert_errors(Vacation(start_date=today, until_revoked=True, end_date=end), {'end_date'})
        self.assert_errors(Vacation(start_date=today), {'end_date'})
        self.assert_errors(Vacation(start_date=today + timedelta(days=5), end_date=end), {'end_date'})
        person = Person(pk=5, first_name='Jan', last_name='Test')
        self.assertEqual(str(Vacation(person=person, start_date=today, until_revoked=True)),
                         f'Jan Test: od {today:%d.%m.%Y} do odwołania')
        self.assertEqual(str(Vacation(start_date=today, end_date=end)),
                         f'bez przypisanej osoby: od {today:%d.%m.%Y} do {end:%d.%m.%Y}, 15:30')
        self.assertEqual(str(Vacation()), 'bez przypisanej osoby: bez daty rozpoczęcia')

    def test_vacation_state_flags_and_naive_dates(self):
        from people.models import normalize_datetime

        today = timezone.localdate()
        self.assertFalse(Vacation(until_revoked=True).is_active)
        planned = Vacation(start_date=today + timedelta(days=1), until_revoked=True)
        self.assertEqual((planned.is_active, planned.is_upcoming, planned.can_be_edited), (False, True, True))
        current = Vacation(start_date=today, end_date=datetime.combine(today + timedelta(days=1), time.min))
        self.assertEqual((current.is_active, current.is_finished, current.can_be_edited), (True, False, True))
        past = Vacation(start_date=today - timedelta(days=3), end_date=timezone.now() - timedelta(days=1))
        self.assertEqual((past.is_active, past.is_finished, past.can_be_edited), (False, True, False))
        naive = datetime(2026, 1, 1, 12, 0)
        self.assertTrue(timezone.is_aware(normalize_datetime(naive)))
        aware = timezone.make_aware(naive)
        self.assertIs(normalize_datetime(aware), aware)


class VacationConflictPermissionTests(TestCase):
    """Strona konfliktu wersji (409) pokazuje przesłane i aktualne dane urlopu, więc dostaje ją tylko osoba, która
    może urlopem zarządzać (właściciel, koordynator, superuser). Pozostali dostają odpowiedź widoku (404)."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_member('vacation-owner', 'Redaktor')
        cls.colleague = create_member('vacation-colleague', 'Korektor')
        cls.coordinator = create_member('vacation-coordinator', 'Koordynator redakcji')
        cls.admin = create_superuser('vacation-admin')
        today = timezone.localdate()
        cls.planned = Vacation.objects.create(person=cls.owner.person_profile, start_date=today + timedelta(days=3),
                                              until_revoked=True)
        cls.current = Vacation.objects.create(person=cls.owner.person_profile, start_date=today, until_revoked=True)

    def test_stale_form_conflict_only_for_users_who_manage_the_vacation(self):
        routes = (('edit_vacation', self.planned), ('end_vacation', self.current), ('cancel_vacation', self.planned))
        expected = ((self.owner, 409), (self.coordinator, 409), (self.admin, 409), (self.colleague, 404))
        for user, status in expected:
            self.client.force_login(user)
            for name, vacation in routes:
                with self.subTest(user=user.username, route=name):
                    response = self.client.post(reverse(f'core:{name}', args=[vacation.pk]),
                                                {'_edit_version': 'nieaktualny', 'start_date': '2000-01-01'})
                    self.assertEqual(response.status_code, status)
        self.assertEqual(Vacation.objects.filter(until_revoked=True).count(), 2)


class TeamAccessHelperTests(TestCase):
    """Pomocniki ról, urlopów i dostępności odporne na brak danych (brak konta, konto niezapisane, pusta rola)."""

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
