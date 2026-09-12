from datetime import date, datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from .admin import PersonAdmin, PersonAdminForm
from .models import (
    Person,
    Role,
    Vacation,
    format_local_datetime,
    get_local_date,
    normalize_datetime,
)


User = get_user_model()


class PeopleTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.editor_role = Role.objects.create(name="Redaktor")
        cls.proofreader_role = Role.objects.create(name="Korektor")
        cls.reviewer_role = Role.objects.create(name="Recenzent")

        cls.editor = Person.objects.create(
            first_name="Anna",
            last_name="Nowak",
            email="anna@example.com",
        )
        cls.editor.roles.add(cls.editor_role)

        cls.proofreader = Person.objects.create(
            first_name="Jan",
            last_name="Kowalski",
            email="jan@example.com",
        )
        cls.proofreader.roles.add(cls.proofreader_role)

        cls.multi_role_person = Person.objects.create(
            first_name="Ewa",
            last_name="Lis",
            email="ewa@example.com",
        )
        cls.multi_role_person.roles.add(
            cls.editor_role,
            cls.proofreader_role,
        )

        cls.reviewer = Person.objects.create(
            first_name="Piotr",
            last_name="Mazur",
            email="piotr@example.com",
        )
        cls.reviewer.roles.add(cls.reviewer_role)

        cls.former_member = Person.objects.create(
            first_name="Maria",
            last_name="Wrona",
            email="maria@example.com",
            is_active=False,
        )
        cls.former_member.roles.add(cls.editor_role)


class PersonQuerySetTests(PeopleTestDataMixin, TestCase):
    def test_active_queryset_excludes_former_members(self):
        self.assertSetEqual(
            set(Person.objects.active().values_list("pk", flat=True)),
            {
                self.editor.pk,
                self.proofreader.pk,
                self.multi_role_person.pk,
                self.reviewer.pk,
            },
        )

    def test_default_manager_preserves_access_to_former_members(self):
        self.assertTrue(
            Person.objects.filter(pk=self.former_member.pk).exists()
        )

    def test_multiple_roles_use_or_and_do_not_duplicate_people(self):
        queryset = Person.objects.active().with_roles(
            [self.editor_role.pk, self.proofreader_role.pk]
        )
        person_ids = list(queryset.values_list("pk", flat=True))

        self.assertCountEqual(
            person_ids,
            [
                self.editor.pk,
                self.proofreader.pk,
                self.multi_role_person.pk,
            ],
        )
        self.assertEqual(person_ids.count(self.multi_role_person.pk), 1)

    def test_empty_role_selection_preserves_active_filter(self):
        queryset = Person.objects.active().with_roles([])

        self.assertSetEqual(
            set(queryset.values_list("pk", flat=True)),
            set(Person.objects.active().values_list("pk", flat=True)),
        )

    def test_role_filter_can_include_former_members_for_admin_use(self):
        queryset = Person.objects.with_roles([self.editor_role.pk])

        self.assertTrue(
            queryset.filter(pk=self.former_member.pk).exists()
        )

    def test_deactivating_person_preserves_roles_and_vacations(self):
        vacation = Vacation.objects.create(
            person=self.editor,
            start_date=date(2026, 1, 1),
            until_revoked=True,
        )

        self.editor.is_active = False
        self.editor.save(update_fields=["is_active"])

        self.assertFalse(
            Person.objects.active().filter(pk=self.editor.pk).exists()
        )
        self.assertTrue(
            Person.objects.filter(pk=self.editor.pk).exists()
        )
        self.assertTrue(
            self.editor.roles.filter(pk=self.editor_role.pk).exists()
        )
        self.assertTrue(
            Vacation.objects.filter(pk=vacation.pk).exists()
        )

    def test_reactivating_person_restores_active_listing(self):
        self.former_member.is_active = True
        self.former_member.save(update_fields=["is_active"])

        self.assertTrue(
            Person.objects.active().filter(
                pk=self.former_member.pk
            ).exists()
        )


class PersonModelTests(TestCase):
    def person_values(self, **overrides):
        values = {
            "first_name": "Anna",
            "last_name": "Nowak",
            "email": "anna@example.com",
        }
        values.update(overrides)
        return values

    def test_exact_duplicate_email_is_rejected_by_database(self):
        Person.objects.create(**self.person_values())

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Person.objects.create(
                    **self.person_values(first_name="Inna")
                )

    def test_required_fields_are_validated(self):
        for field_name in ("first_name", "last_name", "email"):
            with self.subTest(field=field_name):
                person = Person(
                    **self.person_values(**{field_name: ""})
                )

                with self.assertRaises(ValidationError) as error:
                    person.full_clean()

                self.assertIn(field_name, error.exception.message_dict)

    def test_invalid_email_addresses_are_rejected(self):
        for field_name in ("email", "dropbox_email"):
            with self.subTest(field=field_name):
                person = Person(
                    **self.person_values(
                        **{field_name: "niepoprawny-adres"}
                    )
                )

                with self.assertRaises(ValidationError) as error:
                    person.full_clean()

                self.assertIn(field_name, error.exception.message_dict)

    def test_deleting_user_preserves_person(self):
        user = User.objects.create_user(username="czlonek")
        person = Person.objects.create(
            **self.person_values(user=user)
        )

        user.delete()
        person.refresh_from_db()

        self.assertIsNone(person.user_id)
        self.assertTrue(person.is_active)

    def test_one_user_cannot_be_linked_to_multiple_people(self):
        user = User.objects.create_user(username="czlonek")
        Person.objects.create(**self.person_values(user=user))

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Person.objects.create(
                    **self.person_values(
                        email="druga@example.com",
                        user=user,
                    )
                )


class PersonAdminFormTests(PeopleTestDataMixin, TestCase):
    def form_data(self, **overrides):
        data = {
            "first_name": "Tomasz",
            "last_name": "Zielinski",
            "email": "tomasz@example.com",
            "dropbox_email": "",
            "is_active": True,
            "roles": [],
            "is_coordinator": False,
            "user": "",
            "leave_start_date": "",
            "leave_end_date": "",
            "leave_until_revoked": False,
        }
        data.update(overrides)
        return data

    def test_new_person_can_have_multiple_roles(self):
        form = PersonAdminForm(
            data=self.form_data(
                roles=[
                    self.editor_role.pk,
                    self.proofreader_role.pk,
                ]
            )
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        person = form.save()

        self.assertSetEqual(
            set(person.roles.values_list("pk", flat=True)),
            {self.editor_role.pk, self.proofreader_role.pk},
        )

    def test_identity_fields_are_trimmed(self):
        form = PersonAdminForm(
            data=self.form_data(
                first_name="  Tomasz   Adam  ",
                last_name="  Zielinski  ",
                email="  tomasz@example.com  ",
                dropbox_email="  dropbox@example.com  ",
            )
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        person = form.save()

        self.assertEqual(person.first_name, "Tomasz Adam")
        self.assertEqual(person.last_name, "Zielinski")
        self.assertEqual(person.email, "tomasz@example.com")
        self.assertEqual(person.dropbox_email, "dropbox@example.com")

    def test_duplicate_email_check_includes_inactive_people(self):
        for email in (
            self.editor.email.upper(),
            self.former_member.email.upper(),
        ):
            with self.subTest(email=email):
                form = PersonAdminForm(
                    data=self.form_data(email=email)
                )

                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors.as_data()["email"][0].code,
                    "duplicate_email",
                )

    def test_editing_person_does_not_match_own_email(self):
        form = PersonAdminForm(
            instance=self.editor,
            data=self.form_data(
                first_name=self.editor.first_name,
                last_name=self.editor.last_name,
                email=self.editor.email,
                roles=[self.editor_role.pk],
                is_active=False,
            ),
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        self.editor.refresh_from_db()

        self.assertFalse(self.editor.is_active)
        self.assertEqual(self.editor.email, "anna@example.com")

    def test_form_rejects_inconsistent_leave_fields(self):
        form = PersonAdminForm(
            data=self.form_data(
                leave_start_date="2026-01-15",
                leave_until_revoked=False,
            )
        )

        self.assertFalse(form.is_valid())
        self.assertIn("leave_end_date", form.errors)


@override_settings(USE_TZ=True, TIME_ZONE="Europe/Warsaw")
class LeaveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = Person.objects.create(
            first_name="Anna",
            last_name="Urlopowa",
            email="urlop@example.com",
        )

    def setUp(self):
        super().setUp()

        self.timezone_override = timezone.override("Europe/Warsaw")
        self.timezone_override.__enter__()
        self.addCleanup(
            self.timezone_override.__exit__,
            None,
            None,
            None,
        )

        self.now = datetime(
            2026,
            1,
            15,
            12,
            0,
            tzinfo=datetime_timezone.utc,
        )
        patcher = patch("people.models.timezone.now", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def vacation(self, **overrides):
        values = {
            "person": self.person,
            "start_date": date(2026, 1, 15),
            "end_date": self.now + timedelta(hours=1),
            "until_revoked": False,
        }
        values.update(overrides)
        return Vacation(**values)

    def test_vacation_and_person_agree_on_active_leave(self):
        cases = (
            (
                date(2026, 1, 15),
                self.now + timedelta(hours=1),
                False,
                True,
            ),
            (
                date(2026, 1, 15),
                self.now,
                False,
                False,
            ),
            (
                date(2026, 1, 15),
                self.now - timedelta(seconds=1),
                False,
                False,
            ),
            (
                date(2026, 1, 16),
                None,
                True,
                False,
            ),
            (
                date(2026, 1, 15),
                None,
                True,
                True,
            ),
            (
                None,
                None,
                False,
                False,
            ),
        )

        for start, end, indefinite, expected in cases:
            with self.subTest(
                start=start,
                end=end,
                indefinite=indefinite,
            ):
                vacation = self.vacation(
                    start_date=start,
                    end_date=end,
                    until_revoked=indefinite,
                )
                self.person.leave_start_date = start
                self.person.leave_end_date = end
                self.person.leave_until_revoked = indefinite

                self.assertEqual(vacation.is_active, expected)
                self.assertEqual(self.person.is_on_leave, expected)

    def test_finished_vacation_cannot_be_edited_or_ended(self):
        vacation = self.vacation(
            end_date=self.now - timedelta(seconds=1)
        )

        self.assertTrue(vacation.is_finished)
        self.assertFalse(vacation.is_active)
        self.assertFalse(vacation.is_upcoming)
        self.assertFalse(vacation.can_be_edited)
        self.assertFalse(vacation.can_be_ended)

    def test_upcoming_indefinite_vacation_can_be_edited_but_not_ended(self):
        vacation = self.vacation(
            start_date=date(2026, 1, 16),
            end_date=None,
            until_revoked=True,
        )

        self.assertTrue(vacation.is_upcoming)
        self.assertTrue(vacation.can_be_edited)
        self.assertFalse(vacation.can_be_ended)

    def test_active_indefinite_vacation_can_be_ended(self):
        vacation = self.vacation(
            end_date=None,
            until_revoked=True,
        )

        self.assertTrue(vacation.can_be_ended)
        self.assertFalse(vacation.is_finished)

    def test_start_date_uses_local_day_at_midnight_boundary(self):
        utc_time = datetime(
            2026, 1, 14, 23, 30,
            tzinfo=datetime_timezone.utc,
        )

        with patch("people.models.timezone.now", return_value=utc_time):
            vacation = self.vacation(
                start_date=date(2026, 1, 15),
                end_date=None,
                until_revoked=True,
            )

            self.assertTrue(vacation.is_active)
            self.assertFalse(vacation.is_upcoming)

    def test_naive_end_time_uses_current_timezone(self):
        vacation = self.vacation(
            end_date=datetime(2026, 1, 15, 14, 0)
        )

        self.assertTrue(vacation.is_active)
        self.assertFalse(vacation.is_finished)

    def test_invalid_vacation_periods_are_rejected(self):
        cases = (
            (
                {"start_date": None},
                "start_date",
            ),
            (
                {"end_date": None, "until_revoked": False},
                "end_date",
            ),
            (
                {"until_revoked": True},
                "end_date",
            ),
            (
                {"end_date": self.now - timedelta(days=1)},
                "end_date",
            ),
        )

        for overrides, expected_field in cases:
            with self.subTest(overrides=overrides):
                vacation = self.vacation(**overrides)

                with self.assertRaises(ValidationError) as error:
                    vacation.full_clean()

                self.assertIn(
                    expected_field,
                    error.exception.message_dict,
                )

    def test_person_leave_validation_rejects_inconsistent_periods(self):
        cases = (
            (None, self.now, False, "leave_start_date"),
            (None, None, True, "leave_start_date"),
            (date(2026, 1, 15), None, False, "leave_end_date"),
            (date(2026, 1, 15), self.now, True, "leave_end_date"),
            (
                date(2026, 1, 16),
                self.now,
                False,
                "leave_end_date",
            ),
        )

        for start, end, indefinite, expected_field in cases:
            with self.subTest(
                start=start,
                end=end,
                indefinite=indefinite,
            ):
                self.person.leave_start_date = start
                self.person.leave_end_date = end
                self.person.leave_until_revoked = indefinite

                with self.assertRaises(ValidationError) as error:
                    self.person.full_clean()

                self.assertIn(
                    expected_field,
                    error.exception.message_dict,
                )

    def test_valid_period_ending_on_same_local_day_is_accepted(self):
        vacation = self.vacation()
        vacation.full_clean()

        self.person.leave_start_date = vacation.start_date
        self.person.leave_end_date = vacation.end_date
        self.person.leave_until_revoked = False
        self.person.full_clean()

    def test_unsaved_vacation_without_dates_has_safe_string(self):
        vacation = Vacation()

        self.assertIn("bez daty rozpoczęcia", str(vacation))
        self.assertIn("bez przypisanej osoby", str(vacation))

    def test_datetime_helpers_use_local_timezone(self):
        value = datetime(
            2026, 1, 14, 23, 30,
            tzinfo=datetime_timezone.utc,
        )

        self.assertEqual(get_local_date(value), date(2026, 1, 15))
        self.assertEqual(
            format_local_datetime(value),
            "15.01.2026, 00:30",
        )

    @override_settings(USE_TZ=False)
    def test_datetime_normalization_without_timezone_support(self):
        normalized = normalize_datetime(self.now)

        self.assertTrue(timezone.is_naive(normalized))
        self.assertEqual(
            normalized,
            datetime(2026, 1, 15, 13, 0),
        )


class PersonAdminTests(PeopleTestDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.superuser = User.objects.create_superuser(
            username="admin_zespolu",
            email="admin@example.com",
            password="Testowe-haslo-123!",
        )

    def setUp(self):
        super().setUp()

        self.factory = RequestFactory()
        self.model_admin = PersonAdmin(
            Person,
            AdminSite(name="people_test_admin"),
        )

    def request(self, params=None):
        request = self.factory.get(
            "/admin/people/person/",
            data=params or {},
        )
        request.user = self.superuser
        return request

    def test_admin_queryset_includes_former_members(self):
        queryset = self.model_admin.get_queryset(self.request())

        self.assertTrue(
            queryset.filter(pk=self.former_member.pk).exists()
        )

    def test_admin_can_filter_current_and_former_members(self):
        cases = (
            (
                "1",
                {
                    self.editor.pk,
                    self.proofreader.pk,
                    self.multi_role_person.pk,
                    self.reviewer.pk,
                },
            ),
            (
                "0",
                {self.former_member.pk},
            ),
        )

        for value, expected_ids in cases:
            with self.subTest(value=value):
                changelist = self.model_admin.get_changelist_instance(
                    self.request({"is_active__exact": value})
                )

                self.assertSetEqual(
                    set(
                        changelist.queryset.values_list(
                            "pk", flat=True
                        )
                    ),
                    expected_ids,
                )

    def test_rendering_role_column_does_not_query_per_person(self):
        request = self.request()

        with self.assertNumQueries(2):
            people = list(self.model_admin.get_queryset(request))
            role_labels = {
                person.pk: self.model_admin.display_roles(person)
                for person in people
            }

        self.assertEqual(role_labels[self.editor.pk], "Redaktor")
        self.assertEqual(role_labels[self.proofreader.pk], "Korektor")
        self.assertEqual(
            role_labels[self.multi_role_person.pk],
            "Korektor, Redaktor",
        )