"""Urlopy: uprawnienia i reguły dat w usłudze, strona „Moje urlopy”, usuwanie w adminie i zbiorcza dostępność zespołu."""
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.services.vacations import cancel_planned_vacation, create_vacation, finish_vacation, update_vacation
from fantazmaty.testing import create_superuser
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
        self.member.refresh_from_db()
        self.assertTrue(vacation.until_revoked)
        self.assertIsNone(vacation.end_date)
        self.assertEqual(self.member.leave_start_date, self.today)
        self.assertTrue(self.member.leave_until_revoked)
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
        Vacation(person=self.member, start_date=self.today + timedelta(days=2),
                 end_date=midnight + timedelta(days=1)).full_clean()

    def test_planned_vacation_does_not_block_work_yet(self):
        self.create(start_date=self.today + timedelta(days=5), until_revoked=True)
        self.assertFalse(is_on_leave(self.member_user))
        self.member.refresh_from_db()
        self.assertEqual(self.member.leave_start_date, self.today + timedelta(days=5))

    def test_current_vacation_takes_precedence_over_planned_one(self):
        self.create(start_date=self.today + timedelta(days=10), until_revoked=True)
        self.create(start_date=self.today, end_date=self.today + timedelta(days=2))
        self.member.refresh_from_db()
        self.assertEqual(self.member.leave_start_date, self.today)
        self.assertFalse(self.member.leave_until_revoked)

    def test_finish_ends_an_active_vacation_now(self):
        vacation = self.create(until_revoked=True)
        finished = finish_vacation(user=self.member_user, vacation_id=vacation.pk)
        self.assertFalse(finished.until_revoked)
        self.assertLessEqual(finished.end_date, timezone.now())
        self.assertFalse(is_on_leave(self.member_user))
        self.member.refresh_from_db()
        self.assertIsNone(self.member.leave_start_date)

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

    def test_disabled_account_not_in_vacation_choices(self):
        self.member_user.is_active = False
        self.member_user.save(update_fields=['is_active'])
        self.client.force_login(self.coordinator_user)
        page = self.client.get(reverse('core:my_vacations'))
        self.assertNotIn(self.member.pk, [p.pk for p in page.context['vacation_people']])

    def test_my_vacations_show_entire_own_history_without_pagination(self):
        admin = create_superuser('layout57')
        person = Person.objects.create(user=admin, first_name='Jan', last_name='Admin')
        start = timezone.now() - timedelta(days=365)
        Vacation.objects.bulk_create([
            Vacation(person=person, start_date=(start + timedelta(days=i * 3)).date(),
                     end_date=start + timedelta(days=i * 3 + 1)) for i in range(60)
        ])
        Vacation.objects.create(person=self.other, start_date=start.date(), end_date=start + timedelta(days=1))
        self.client.force_login(admin)
        response = self.client.get(reverse('core:my_vacations'), {'page': 2, 'page_size': 25})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['vacations']), 60)
        self.assertTrue(all(v.person_id == person.pk for v in response.context['vacations']))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[@id="my-vacations-table"]/tbody/tr')), 60)
        self.assertEqual(doc.xpath('//*[@id="my-vacations-table"]/@data-pagination'), ['off'])
        self.assertFalse(doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," cms-pagination ")]'))

    def test_admin_deletes_planned_vacation_of_former_member(self):
        from core.admin import VacationAdmin

        request = RequestFactory().post('/')
        request.user = create_superuser()
        vacation = Vacation.objects.create(person=self.other, start_date=self.today + timedelta(days=10),
                                           end_date=timezone.now() + timedelta(days=12))
        self.other.is_active = False
        self.other.save()
        VacationAdmin(Vacation, site).delete_model(request, vacation)
        self.assertFalse(Vacation.objects.filter(pk=vacation.pk).exists())

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
