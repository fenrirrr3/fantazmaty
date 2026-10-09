"""Vacation service: permissions, date rules and the person's leave summary."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from core.services.vacations import cancel_planned_vacation, create_vacation, finish_vacation, update_vacation
from people.leave_access import is_on_leave, require_available
from people.models import Person, Role, Vacation


class VacationServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.member_user = User.objects.create_user('member', 'member@example.test', 'x')
        cls.member = Person.objects.create(user=cls.member_user, first_name='Jan', last_name='Test', email=cls.member_user.email)
        cls.other_user = User.objects.create_user('other', 'other@example.test', 'x')
        cls.other = Person.objects.create(user=cls.other_user, first_name='Anna', last_name='Test', email=cls.other_user.email)
        cls.coordinator_user = User.objects.create_user('coordinator', 'coordinator@example.test', 'x')
        coordinator = Person.objects.create(user=cls.coordinator_user, first_name='Ewa', last_name='Test', email=cls.coordinator_user.email)
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
            update_vacation(user=self.member_user, vacation_id=vacation.pk, start_date=self.today, until_revoked=True)

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
