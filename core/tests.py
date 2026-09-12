from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from authors.models import Author, AuthorNote
from people.models import Person, Role, Vacation
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage

from .forms import (
    AuthorNoteForm,
    AuthorNotificationForm,
    CompleteStageForm,
    CoordinatorReviewBulkActionForm,
    CoordinatorTextBulkActionForm,
    GlobalSearchForm,
    PeopleFilterForm,
    RestartWorkflowForm,
    ReviewBulkImportForm,
    ReviewerOpinionForm,
    StartStageForm,
    TextNoteForm,
    VacationForm,
)
from .permissions import (
    get_active_person_profile,
    has_role,
    is_coordinator,
)


User = get_user_model()


def create_member(username, role_name=None, *, coordinator=False):
    user = User.objects.create_user(username=username)
    person = Person.objects.create(
        first_name=username,
        last_name="Zespolowy",
        email=f"{username}@example.com",
        user=user,
        is_coordinator=coordinator,
    )

    if role_name:
        role, _ = Role.objects.get_or_create(name=role_name)
        person.roles.add(role)

    return user, person


class CoreTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.superuser = User.objects.create_superuser(
            username="superadmin",
            email="superadmin@example.com",
            password="Testowe-haslo-123!",
        )
        cls.reviewer, cls.reviewer_person = create_member(
            "recenzent",
            "Recenzent",
        )
        cls.editor, cls.editor_person = create_member(
            "redaktor",
            "Redaktor",
        )
        cls.coordinator, cls.coordinator_person = create_member(
            "koordynator",
            coordinator=True,
        )
        cls.author = Author.objects.create(
            first_name="PoufneImie",
            last_name="PoufneNazwisko",
            pseudonym="PoufnyPseudonim",
            email="poufny.autor@example.com",
        )
        cls.anthology = Anthology.objects.create(
            title="Antologia testowa"
        )


class PermissionTests(CoreTestDataMixin, TestCase):
    def test_anonymous_user_has_no_roles_or_profile(self):
        user = AnonymousUser()

        self.assertIsNone(get_active_person_profile(user))
        self.assertFalse(has_role(user, "Redaktor"))
        self.assertFalse(is_coordinator(user))

    def test_active_person_role_is_recognized(self):
        self.assertTrue(has_role(self.editor, "Redaktor"))
        self.assertFalse(has_role(self.editor, "Korektor"))

    def test_coordinator_flag_is_recognized(self):
        self.assertTrue(is_coordinator(self.coordinator))

    def test_coordinator_role_is_recognized(self):
        role, _ = Role.objects.get_or_create(name="Koordynator")
        self.editor_person.roles.add(role)

        self.assertTrue(is_coordinator(self.editor))

    def test_specialized_coordinator_role_has_coordinator_permissions(self):
        role, _ = Role.objects.get_or_create(name="Koordynator ilustracji")
        self.editor_person.roles.add(role)

        self.assertTrue(is_coordinator(self.editor))
        self.assertTrue(has_role(self.editor, "Koordynator"))

    def test_former_member_does_not_gain_access_from_old_group(self):
        group, _ = Group.objects.get_or_create(name="Koordynator")
        self.editor.groups.add(group)
        self.editor_person.is_active = False
        self.editor_person.save(update_fields=["is_active"])

        self.assertIsNone(get_active_person_profile(self.editor))
        self.assertFalse(has_role(self.editor, "Redaktor"))
        self.assertFalse(is_coordinator(self.editor))

    def test_inactive_account_has_no_operational_permissions(self):
        self.coordinator.is_active = False
        self.coordinator.save(update_fields=["is_active"])

        self.assertIsNone(get_active_person_profile(self.coordinator))
        self.assertFalse(is_coordinator(self.coordinator))

    def test_active_superuser_does_not_require_person_profile(self):
        self.assertTrue(is_coordinator(self.superuser))
        self.assertTrue(has_role(self.superuser, "Redaktor"))

    def test_inactive_superuser_is_denied(self):
        self.superuser.is_active = False
        self.superuser.save(update_fields=["is_active"])

        self.assertFalse(is_coordinator(self.superuser))
        self.assertFalse(has_role(self.superuser, "Redaktor"))


class WorkflowFormTests(TestCase):
    def test_start_form_rejects_past_and_distant_dates(self):
        today = timezone.localdate()

        for value in (
            today - timedelta(days=1),
            today + timedelta(days=15),
        ):
            with self.subTest(value=value):
                form = StartStageForm(
                    data={"started_at": value.isoformat()}
                )

                self.assertFalse(form.is_valid())
                self.assertIn("started_at", form.errors)

    def test_start_form_accepts_fourteen_day_boundary(self):
        value = timezone.localdate() + timedelta(days=14)
        form = StartStageForm(
            data={"started_at": value.isoformat()}
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_completion_cannot_precede_stage_start(self):
        today = timezone.localdate()
        stage = WorkflowStage(started_at=today)
        form = CompleteStageForm(
            data={"ended_at": (today - timedelta(days=1)).isoformat()},
            stage=stage,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("ended_at", form.errors)

    def test_restart_form_excludes_terminal_stages(self):
        for stage_type in (
            WorkflowStage.StageType.READY,
            WorkflowStage.StageType.WITHDRAWN,
        ):
            with self.subTest(stage=stage_type):
                form = RestartWorkflowForm(
                    data={"target_stage": stage_type}
                )

                self.assertFalse(form.is_valid())
                self.assertIn("target_stage", form.errors)


class ContentFormTests(TestCase):
    def test_blank_notes_are_rejected(self):
        for form_class in (TextNoteForm, AuthorNoteForm):
            with self.subTest(form=form_class.__name__):
                form = form_class(data={"content": " \n\t "})

                self.assertFalse(form.is_valid())
                self.assertIn("content", form.errors)

    def test_review_opinion_requires_notes(self):
        form = ReviewerOpinionForm(
            data={
                "opinion": ReviewAssignment.Opinion.YES,
                "notes": " \n ",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("notes", form.errors)

    def test_reading_is_not_a_final_opinion(self):
        form = ReviewerOpinionForm(
            data={
                "opinion": ReviewAssignment.Opinion.READING,
                "notes": "Przykładowa treść recenzji.",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("opinion", form.errors)

    def test_notification_date_defaults_to_today(self):
        form = AuthorNotificationForm(
            data={"author_notified": "on"}
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(
            form.cleaned_data["author_notified_at"],
            timezone.localdate(),
        )

    def test_unchecked_notification_clears_date(self):
        form = AuthorNotificationForm(
            data={
                "author_notified_at": timezone.localdate().isoformat(),
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertIsNone(form.cleaned_data["author_notified_at"])

    def test_invalid_notification_date_is_not_silently_replaced(self):
        form = AuthorNotificationForm(
            data={
                "author_notified": "on",
                "author_notified_at": "niepoprawna-data",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("author_notified_at", form.errors)

    def test_search_rejects_whitespace_only(self):
        form = GlobalSearchForm(data={"query": " \t "})

        self.assertFalse(form.is_valid())
        self.assertIn("query", form.errors)


class VacationFormTests(CoreTestDataMixin, TestCase):
    def test_new_vacation_cannot_start_in_past(self):
        form = VacationForm(
            data={
                "start_date": (
                    timezone.localdate() - timedelta(days=1)
                ).isoformat(),
                "until_revoked": "on",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("start_date", form.errors)

    def test_existing_active_vacation_can_preserve_original_start(self):
        start = timezone.localdate() - timedelta(days=3)
        vacation = Vacation.objects.create(
            person=self.editor_person,
            start_date=start,
            until_revoked=True,
        )
        form = VacationForm(
            instance=vacation,
            data={
                "start_date": start.isoformat(),
                "until_revoked": "on",
            },
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_indefinite_vacation_clears_supplied_end(self):
        today = timezone.localdate()
        form = VacationForm(
            data={
                "start_date": today.isoformat(),
                "end_date": f"{today.isoformat()}T18:00",
                "until_revoked": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertIsNone(form.cleaned_data["end_date"])

    def test_fixed_vacation_requires_end(self):
        form = VacationForm(
            data={"start_date": timezone.localdate().isoformat()}
        )

        self.assertFalse(form.is_valid())
        self.assertIn("end_date", form.errors)


class TeamAndBulkFormTests(CoreTestDataMixin, TestCase):
    def test_people_filter_accepts_multiple_roles(self):
        editor_role = Role.objects.get(name="Redaktor")
        proofreader_role = Role.objects.create(name="Korektor")

        form = PeopleFilterForm(
            data={
                "roles": [editor_role.pk, proofreader_role.pk],
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertSetEqual(
            set(form.cleaned_data["roles"].values_list("pk", flat=True)),
            {editor_role.pk, proofreader_role.pk},
        )

    def test_people_filter_rejects_unknown_role(self):
        form = PeopleFilterForm(
            data={"roles": [2147483647]}
        )

        self.assertFalse(form.is_valid())
        self.assertIn("roles", form.errors)

    def test_bulk_assignment_rejects_former_member(self):
        self.editor_person.is_active = False
        self.editor_person.save(update_fields=["is_active"])

        form = CoordinatorTextBulkActionForm(
            data={
                "action": "reserve_role",
                "role": WorkflowRoleAssignment.Role.EDITOR,
                "assigned_to": self.editor.pk,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("assigned_to", form.errors)

    def test_bulk_assignment_requires_matching_role(self):
        form = CoordinatorTextBulkActionForm(
            data={
                "action": "reserve_role",
                "role": WorkflowRoleAssignment.Role.PROOFREADER_1,
                "assigned_to": self.editor.pk,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("assigned_to", form.errors)

    def test_bulk_note_requires_nonempty_content(self):
        form = CoordinatorTextBulkActionForm(
            data={"action": "add_note", "note": " \n "}
        )

        self.assertFalse(form.is_valid())
        self.assertIn("note", form.errors)

    def test_bulk_status_change_requires_status(self):
        form = CoordinatorReviewBulkActionForm(
            data={"action": "change_status"}
        )

        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)


class ReviewImportFormTests(CoreTestDataMixin, TestCase):
    def record(
        self,
        *,
        title="Nowe opowiadanie",
        email="nowy.autor@example.com",
        length="25 000",
        name="Jan Nowak",
    ):
        return (
            f"[{name}];[{title}];[fantasy];[{length}];"
            f"[przemoc];[{email}];[123456789]"
        )

    def form(self, records, *, user=None, **overrides):
        data = {
            "anthology": str(self.anthology.pk),
            "records": records,
        }
        data.update(overrides)

        return ReviewBulkImportForm(
            data=data,
            user=self.superuser if user is None else user,
        )

    def confirmed_form(self, original_form, **overrides):
        data = original_form.data.copy()
        data["confirm_submission_warnings"] = "on"
        data.update(overrides)

        return ReviewBulkImportForm(
            data=data,
            user=self.superuser,
        )

    def test_valid_import_is_parsed_without_writing_to_database(self):
        review_count = Review.objects.count()
        author_count = Author.objects.count()
        form = self.form(self.record())

        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(len(form.parsed_records), 1)
        self.assertEqual(form.parsed_records[0]["length"], 25000)
        self.assertEqual(
            form.parsed_records[0]["author_first_name"],
            "Jan",
        )
        self.assertEqual(Review.objects.count(), review_count)
        self.assertEqual(Author.objects.count(), author_count)

    def test_coordinator_cannot_validate_author_import(self):
        form = self.form(self.record(), user=self.coordinator)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.parsed_records, [])

    def test_missing_user_is_denied(self):
        form = ReviewBulkImportForm(
            data={
                "anthology": self.anthology.pk,
                "records": self.record(),
            }
        )

        self.assertFalse(form.is_valid())

    def test_invalid_rows_reject_entire_batch(self):
        form = self.form(
            self.record() + "\n" + "niepoprawny drugi wiersz"
        )

        self.assertFalse(form.is_valid())
        self.assertIn("records", form.errors)
        self.assertEqual(form.parsed_records, [])

    def test_out_of_range_lengths_are_rejected(self):
        for length in ("0", "-1", "2147483648", "9" * 100):
            with self.subTest(length=length):
                form = self.form(self.record(length=length))

                self.assertFalse(form.is_valid())
                self.assertIn("records", form.errors)

    def test_model_field_length_limits_are_checked(self):
        form = self.form(self.record(title="x" * 256))

        self.assertFalse(form.is_valid())
        self.assertIn("records", form.errors)

    def test_blacklist_requires_previously_displayed_warning(self):
        self.author.is_blacklisted = True
        self.author.save(update_fields=["is_blacklisted"])
        form = self.form(
            self.record(email=self.author.email),
            confirm_submission_warnings="on",
        )

        self.assertFalse(form.is_valid())
        self.assertTrue(form.import_warnings)
        self.assertIn("confirm_submission_warnings", form.errors)

        confirmed = self.confirmed_form(form)

        self.assertTrue(confirmed.is_valid(), confirmed.errors.as_json())
        from core.services.reviews import import_reviews
        import_reviews(user=self.superuser, form=confirmed)
        created = Review.objects.latest("pk")
        self.assertTrue(created.is_hidden)
        self.assertEqual(created.status, Review.Status.REJECTED)
        self.assertTrue(14 <= (created.decision_at - timezone.localdate()).days <= 28)

    def test_duplicate_inside_batch_requires_confirmation(self):
        form = self.form(
            self.record(title="Tytuł")
            + "\n"
            + self.record(title="  TYTUŁ  ")
        )

        self.assertFalse(form.is_valid())
        self.assertTrue(form.import_warnings)
        self.assertIn("confirm_submission_warnings", form.errors)

        confirmed = self.confirmed_form(form)

        self.assertTrue(confirmed.is_valid(), confirmed.errors.as_json())
        self.assertEqual(len(confirmed.parsed_records), 2)

    def test_archived_review_in_other_anthology_is_detected(self):
        other_anthology = Anthology.objects.create(title="Dawny nabór")
        Review.objects.create(
            author=self.author,
            author_first_name=self.author.first_name,
            author_last_name=self.author.last_name,
            email=self.author.email,
            title="Dawne opowiadanie",
            genre="fantasy",
            length=25000,
            anthology=other_anthology,
            old_reviews=True,
        )

        form = self.form(
            self.record(
                title="DAWNE OPOWIADANIE",
                email=self.author.email,
            )
        )

        self.assertFalse(form.is_valid())
        self.assertIn("confirm_submission_warnings", form.errors)

    def test_changing_batch_invalidates_warning_confirmation(self):
        self.author.is_blacklisted = True
        self.author.save(update_fields=["is_blacklisted"])
        form = self.form(self.record(email=self.author.email))
        self.assertFalse(form.is_valid())

        changed = self.confirmed_form(
            form,
            records=self.record(
                email=self.author.email,
                title="Zmieniony tytuł",
            ),
        )

        self.assertFalse(changed.is_valid())
        self.assertIn("confirm_submission_warnings", changed.errors)

    def test_confirmation_is_bound_to_importing_user(self):
        self.author.is_blacklisted = True
        self.author.save(update_fields=["is_blacklisted"])
        form = self.form(self.record(email=self.author.email))
        self.assertFalse(form.is_valid())

        other_superuser = User.objects.create_superuser(
            username="inny_superadmin",
            email="inny.superadmin@example.com",
            password="Inne-testowe-haslo-123!",
        )
        data = form.data.copy()
        data["confirm_submission_warnings"] = "on"
        changed = ReviewBulkImportForm(
            data=data,
            user=other_superuser,
        )

        self.assertFalse(changed.is_valid())
        self.assertIn("confirm_submission_warnings", changed.errors)


@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage.StaticFilesStorage"
            ),
        },
    },
)
class CoreAccessTests(CoreTestDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.text = Text.objects.create(
            title="Testowy tekst",
            length=25000,
            anthology=cls.anthology,
        )
        cls.text.authors.add(cls.author)
        cls.review = Review.objects.create(
            author=cls.author,
            author_first_name=cls.author.first_name,
            author_last_name=cls.author.last_name,
            email=cls.author.email,
            title="Testowe zgłoszenie",
            genre="fantasy",
            length=25000,
            anthology=cls.anthology,
        )
        ReviewAssignment.objects.create(
            review=cls.review,
            user=cls.reviewer,
            position=1,
        )

    def test_anonymous_user_cannot_open_private_pages(self):
        for name in (
            "home",
            "text_list",
            "review_list",
            "people_list",
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"core:{name}"))

                self.assertEqual(response.status_code, 302)
                self.assertIn("next=", response.url)

    def test_coordinator_cannot_open_author_pages_or_import(self):
        self.client.force_login(self.coordinator)

        urls = (
            reverse("core:author_list"),
            reverse("core:author_detail", args=[self.author.pk]),
            reverse("core:review_bulk_import"),
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 403)
                self.assertNotContains(
                    response,
                    self.author.email,
                    status_code=403,
                )

    def test_coordinator_cannot_add_author_note_by_direct_post(self):
        self.client.force_login(self.coordinator)

        response = self.client.post(
            reverse("core:add_author_note", args=[self.author.pk]),
            {"content": "Niedozwolona notatka."},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AuthorNote.objects.filter(
                content="Niedozwolona notatka."
            ).exists()
        )

    def test_assigned_reviewer_does_not_see_author_identity(self):
        self.client.force_login(self.reviewer)

        response = self.client.get(
            reverse(
                "core:assigned_review_detail",
                args=[self.review.pk],
            )
        )

        self.assertEqual(response.status_code, 200)

        for value in (
            self.author.first_name,
            self.author.last_name,
            self.author.pseudonym,
            self.author.email,
        ):
            self.assertNotContains(response, value)

    def test_review_list_does_not_expose_author_to_coordinator(self):
        self.client.force_login(self.coordinator)

        response = self.client.get(reverse("core:review_list"))

        self.assertEqual(response.status_code, 200)

        for value in (
            self.author.first_name,
            self.author.last_name,
            self.author.email,
        ):
            self.assertNotContains(response, value)

    def test_search_does_not_expose_author_to_coordinator(self):
        self.client.force_login(self.coordinator)

        response = self.client.get(
            reverse("core:global_search"),
            {"query": self.review.title},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.author.email)
        self.assertNotContains(response, self.author.last_name)

    def test_people_list_hides_former_members(self):
        Person.objects.create(
            first_name="BylyCzlonek",
            last_name="NiewidocznyNaLiscie",
            email="byly@example.com",
            is_active=False,
        )
        self.client.force_login(self.editor)

        response = self.client.get(reverse("core:people_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "NiewidocznyNaLiscie")
        self.assertNotContains(response, "byly@example.com")

    def test_mutation_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coordinator)

        response = client.post(
            reverse("core:update_review_status", args=[self.review.pk]),
            {"status": Review.Status.ACCEPTED},
        )

        self.assertEqual(response.status_code, 403)

        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.NEW)

    def test_mutation_routes_reject_get(self):
        self.client.force_login(self.superuser)

        targets = (
            ("bulk_text_action", []),
            ("bulk_review_action", []),
            ("set_text_authors", [self.text.pk]),
            ("assign_reviewer", [self.review.pk]),
            ("unassign_reviewer", [self.review.pk]),
            ("update_review_status", [self.review.pk]),
            ("update_review_content_warnings", [self.review.pk]),
            ("update_author_notification", [self.review.pk]),
            ("copy_review_to_text", [self.review.pk]),
            ("add_text_note", [self.text.pk]),
            ("update_coordinator_note", [self.text.pk]),
            ("update_text_content_warnings", [self.text.pk]),
            ("restart_text_workflow", [self.text.pk]),
            ("withdraw_text", [self.text.pk]),
        )

        for name, args in targets:
            with self.subTest(route=name):
                response = self.client.get(
                    reverse(f"core:{name}", args=args)
                )

                self.assertEqual(response.status_code, 405)
