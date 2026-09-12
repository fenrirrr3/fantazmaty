from datetime import datetime, timezone as datetime_timezone

from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from .admin import (
    AuthorAdmin,
    AuthorAdminForm,
    AuthorNoteAdmin,
    AuthorNoteInline,
)
from .models import Author, AuthorNote


User = get_user_model()

test_admin_site = AdminSite(name="authors_test_admin")
test_admin_site.register(Author, AuthorAdmin)
test_admin_site.register(AuthorNote, AuthorNoteAdmin)

urlpatterns = [
    path("admin/", test_admin_site.urls),
    path("", include("core.urls")),
]


class AuthorModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(
            first_name="Anna",
            last_name="Kowalska",
            pseudonym="A.K.",
            email="anna@example.com",
        )

    def test_new_author_is_not_blacklisted(self):
        self.assertFalse(self.author.is_blacklisted)

    def test_blacklisting_does_not_change_contact_or_contract(self):
        self.author.is_blacklisted = True
        self.author.save(update_fields=["is_blacklisted"])
        self.author.refresh_from_db()

        self.assertTrue(self.author.is_blacklisted)
        self.assertTrue(self.author.contact)
        self.assertFalse(self.author.has_contract)
        self.assertEqual(self.author.email, "anna@example.com")

    def test_string_representation_does_not_expose_blacklist_flag(self):
        self.author.is_blacklisted = True

        self.assertEqual(str(self.author), "Anna Kowalska")

    def test_exact_duplicate_email_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Author.objects.create(
                    first_name="Jan",
                    last_name="Nowak",
                    email=self.author.email,
                )

    def test_invalid_or_missing_required_values_are_rejected(self):
        cases = (
            ("first_name", ""),
            ("last_name", ""),
            ("email", ""),
            ("email", "niepoprawny-adres"),
            ("first_name", "a" * 101),
            ("last_name", "a" * 101),
            ("pseudonym", "a" * 101),
        )

        for field_name, value in cases:
            with self.subTest(field=field_name, value=value):
                values = {
                    "first_name": "Jan",
                    "last_name": "Nowak",
                    "email": "nowy@example.com",
                }
                values[field_name] = value
                author = Author(**values)

                with self.assertRaises(ValidationError) as error:
                    author.full_clean()

                self.assertIn(field_name, error.exception.message_dict)


class AuthorAdminFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(
            first_name="Anna",
            last_name="Kowalska",
            pseudonym="AK",
            email="anna@example.com",
        )

    def form_data(self, **overrides):
        data = {
            "first_name": "Jan",
            "last_name": "Nowak",
            "pseudonym": "",
            "email": "jan@example.com",
            "has_contract": False,
            "contact": True,
            "is_blacklisted": False,
            "confirm_possible_duplicate": False,
        }
        data.update(overrides)
        return data

    def test_unique_author_can_be_created_without_confirmation(self):
        form = AuthorAdminForm(data=self.form_data())

        self.assertTrue(form.is_valid(), form.errors.as_json())

        author = form.save()

        self.assertEqual(author.email, "jan@example.com")
        self.assertFalse(author.is_blacklisted)

    def test_form_normalizes_whitespace_in_identity_fields(self):
        form = AuthorAdminForm(
            data=self.form_data(
                first_name="  Jan   Adam  ",
                last_name="  Nowak  ",
                pseudonym="  J.   N.  ",
                email="  jan@example.com  ",
            )
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())

        author = form.save()

        self.assertEqual(author.first_name, "Jan Adam")
        self.assertEqual(author.last_name, "Nowak")
        self.assertEqual(author.pseudonym, "J. N.")
        self.assertEqual(author.email, "jan@example.com")

    def test_matching_name_requires_confirmation(self):
        form = AuthorAdminForm(
            data=self.form_data(
                first_name="anna",
                last_name="kowalska",
            )
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors.as_data()["confirm_possible_duplicate"][0].code,
            "possible_duplicate_author",
        )
        self.assertEqual(Author.objects.count(), 1)

    def test_matching_pseudonym_requires_confirmation(self):
        form = AuthorAdminForm(
            data=self.form_data(pseudonym="ak")
        )

        self.assertFalse(form.is_valid())
        self.assertIn("confirm_possible_duplicate", form.errors)

    def test_confirmed_similar_name_can_be_saved(self):
        form = AuthorAdminForm(
            data=self.form_data(
                first_name="Anna",
                last_name="Kowalska",
                confirm_possible_duplicate=True,
            )
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()

        self.assertEqual(Author.objects.count(), 2)

    def test_confirmation_cannot_bypass_duplicate_email(self):
        for email in ("anna@example.com", "ANNA@EXAMPLE.COM"):
            with self.subTest(email=email):
                form = AuthorAdminForm(
                    data=self.form_data(
                        email=email,
                        confirm_possible_duplicate=True,
                    )
                )

                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors.as_data()["email"][0].code,
                    "duplicate_email",
                )

        self.assertEqual(Author.objects.count(), 1)

    def test_editing_author_does_not_match_itself(self):
        form = AuthorAdminForm(
            instance=self.author,
            data=self.form_data(
                first_name=self.author.first_name,
                last_name=self.author.last_name,
                pseudonym=self.author.pseudonym,
                email="zmieniony@example.com",
            ),
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()

        self.assertEqual(Author.objects.count(), 1)

    def test_blacklist_change_does_not_require_duplicate_confirmation(self):
        Author.objects.create(
            first_name=self.author.first_name,
            last_name=self.author.last_name,
            email="inna.anna@example.com",
        )

        form = AuthorAdminForm(
            instance=self.author,
            data=self.form_data(
                first_name=self.author.first_name,
                last_name=self.author.last_name,
                pseudonym=self.author.pseudonym,
                email=self.author.email,
                is_blacklisted=True,
            ),
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        self.author.refresh_from_db()

        self.assertTrue(self.author.is_blacklisted)

    def test_identity_change_rechecks_similar_authors(self):
        Author.objects.create(
            first_name="Jan",
            last_name="Nowak",
            email="istniejacy.jan@example.com",
        )

        form = AuthorAdminForm(
            instance=self.author,
            data=self.form_data(email=self.author.email),
        )

        self.assertFalse(form.is_valid())
        self.assertIn("confirm_possible_duplicate", form.errors)


class AuthorNoteModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(
            first_name="Jan",
            last_name="Nowak",
            email="notatki@example.com",
        )
        cls.user = User.objects.create_user(
            username="tworca_notatki",
            first_name="Maria",
            last_name="Zielinska",
        )

    def create_note(self, **overrides):
        values = {
            "author": self.author,
            "created_by": self.user,
            "content": "Przykładowa notatka testowa.",
        }
        values.update(overrides)
        return AuthorNote.objects.create(**values)

    def test_note_content_is_required(self):
        note = AuthorNote(author=self.author, content="")

        with self.assertRaises(ValidationError) as error:
            note.full_clean()

        self.assertIn("content", error.exception.message_dict)

    def test_deleting_creator_preserves_note(self):
        note = self.create_note()

        self.user.delete()
        note.refresh_from_db()

        self.assertIsNone(note.created_by_id)
        self.assertEqual(note.content, "Przykładowa notatka testowa.")
        self.assertIn("nieznany autor notatki", str(note))

    def test_deleting_author_deletes_notes(self):
        note = self.create_note()
        note_id = note.pk

        self.author.delete()

        self.assertFalse(AuthorNote.objects.filter(pk=note_id).exists())

    def test_string_uses_local_time(self):
        note = self.create_note()
        fixed_time = datetime(
            2026, 1, 15, 12, 30,
            tzinfo=datetime_timezone.utc,
        )
        AuthorNote.objects.filter(pk=note.pk).update(
            created_at=fixed_time
        )
        note.refresh_from_db()

        with timezone.override("Europe/Warsaw"):
            self.assertIn("15.01.2026 13:30", str(note))
            self.assertIn("Maria Zielinska", str(note))

    def test_string_falls_back_to_username(self):
        self.user.first_name = ""
        self.user.last_name = ""
        self.user.save(update_fields=["first_name", "last_name"])

        note = self.create_note()

        self.assertIn(self.user.get_username(), str(note))

    def test_unsaved_note_handles_missing_date_and_creator(self):
        note = AuthorNote(
            author=self.author,
            content="Niezapisana notatka.",
        )

        self.assertIn("bez daty", str(note))
        self.assertIn("nieznany autor notatki", str(note))

    def test_equal_timestamps_are_ordered_by_descending_id(self):
        older = self.create_note(content="Pierwsza.")
        newer = self.create_note(content="Druga.")

        AuthorNote.objects.filter(
            pk__in=[older.pk, newer.pk]
        ).update(created_at=older.created_at)

        self.assertEqual(
            list(self.author.notes.values_list("pk", flat=True)),
            [newer.pk, older.pk],
        )


@override_settings(
    ROOT_URLCONF=__name__,
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
class AuthorAdminSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="superadmin",
            email="superadmin@example.com",
            password="Testowe-haslo-123!",
        )
        cls.coordinator = User.objects.create_user(
            username="koordynator",
            is_staff=True,
        )
        cls.regular_user = User.objects.create_user(
            username="zwykly_uzytkownik",
        )

        group = Group.objects.create(name="Koordynator")
        group.permissions.set(
            Permission.objects.filter(
                content_type__app_label="authors"
            )
        )
        cls.coordinator.groups.add(group)

        cls.author = Author.objects.create(
            first_name="Anna",
            last_name="Poufna",
            email="poufna@example.com",
        )
        cls.blacklisted_author = Author.objects.create(
            first_name="Jan",
            last_name="Zastrzezony",
            email="zastrzezony@example.com",
            is_blacklisted=True,
        )
        cls.note = AuthorNote.objects.create(
            author=cls.author,
            created_by=cls.superuser,
            content="Poufna treść notatki testowej.",
        )

    def admin_url(self, name, *args):
        return reverse(
            f"authors_test_admin:{name}",
            args=args or None,
        )

    def test_coordinator_with_model_permissions_cannot_access_admin_data(self):
        self.assertTrue(
            self.coordinator.has_perm("authors.view_author")
        )
        self.client.force_login(self.coordinator)

        urls = (
            self.admin_url("authors_author_changelist"),
            self.admin_url("authors_author_add"),
            self.admin_url("authors_author_change", self.author.pk),
            self.admin_url("authors_author_delete", self.author.pk),
            self.admin_url("authors_author_history", self.author.pk),
            self.admin_url("authors_author_blacklist"),
            self.admin_url("authors_authornote_changelist"),
            self.admin_url("authors_authornote_add"),
            self.admin_url("authors_authornote_change", self.note.pk),
            self.admin_url("authors_authornote_delete", self.note.pk),
            self.admin_url("authors_authornote_history", self.note.pk),
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
                self.assertNotContains(
                    response,
                    self.note.content,
                    status_code=403,
                )

    def test_coordinator_cannot_modify_or_delete_data_by_post(self):
        self.client.force_login(self.coordinator)

        cases = (
            (
                self.admin_url("authors_author_add"),
                {
                    "first_name": "Nowy",
                    "last_name": "Autor",
                    "email": "niedozwolony@example.com",
                },
            ),
            (
                self.admin_url("authors_author_change", self.author.pk),
                {"is_blacklisted": "on", "_save": "Zapisz"},
            ),
            (
                self.admin_url("authors_author_delete", self.author.pk),
                {"post": "yes"},
            ),
            (
                self.admin_url("authors_authornote_add"),
                {
                    "author": self.author.pk,
                    "content": "Niedozwolona notatka.",
                },
            ),
            (
                self.admin_url("authors_authornote_change", self.note.pk),
                {"content": "Niedozwolona zmiana."},
            ),
            (
                self.admin_url("authors_authornote_delete", self.note.pk),
                {"post": "yes"},
            ),
            (
                self.admin_url("authors_author_changelist"),
                {
                    "action": "delete_selected",
                    "_selected_action": str(self.author.pk),
                    "post": "yes",
                },
            ),
        )

        for url, payload in cases:
            with self.subTest(url=url):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 403)

        self.author.refresh_from_db()
        self.note.refresh_from_db()

        self.assertFalse(self.author.is_blacklisted)
        self.assertEqual(
            self.note.content,
            "Poufna treść notatki testowej.",
        )
        self.assertEqual(Author.objects.count(), 2)
        self.assertEqual(AuthorNote.objects.count(), 1)

    def test_author_autocomplete_is_denied_to_coordinator(self):
        self.client.force_login(self.coordinator)

        response = self.client.get(
            self.admin_url("autocomplete"),
            {
                "app_label": "authors",
                "model_name": "authornote",
                "field_name": "author",
                "term": "Anna",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotContains(
            response,
            self.author.email,
            status_code=403,
        )

    def test_anonymous_and_non_staff_users_are_redirected_to_login(self):
        url = self.admin_url("authors_author_changelist")
        login_url = self.admin_url("login")

        for user in (None, self.regular_user):
            with self.subTest(user=user):
                self.client.logout()

                if user is not None:
                    self.client.force_login(user)

                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(login_url))

    def test_permission_checks_reject_inactive_or_non_staff_superuser(self):
        factory = RequestFactory()
        request = factory.get("/admin/")

        candidates = (
            AnonymousUser(),
            self.coordinator,
            User(
                username="inactive_superuser",
                is_superuser=True,
                is_staff=True,
                is_active=False,
            ),
            User(
                username="non_staff_superuser",
                is_superuser=True,
                is_staff=False,
                is_active=True,
            ),
        )
        model_admins = (
            AuthorAdmin(Author, test_admin_site),
            AuthorNoteAdmin(AuthorNote, test_admin_site),
            AuthorNoteInline(Author, test_admin_site),
        )

        for user in candidates:
            request.user = user

            for model_admin in model_admins:
                with self.subTest(
                    user=str(user),
                    admin=type(model_admin).__name__,
                ):
                    self.assertFalse(
                        model_admin.has_view_permission(request)
                    )
                    self.assertFalse(
                        model_admin.has_add_permission(request)
                    )
                    self.assertFalse(
                        model_admin.has_change_permission(request)
                    )
                    self.assertFalse(
                        model_admin.has_delete_permission(request)
                    )
                    self.assertFalse(
                        model_admin.get_queryset(request).exists()
                    )

    def test_superuser_can_view_author_and_note(self):
        self.client.force_login(self.superuser)

        response = self.client.get(
            self.admin_url("authors_author_change", self.author.pk)
        )

        self.assertContains(response, self.author.email)
        self.assertContains(response, self.note.content)

    def test_blacklist_view_only_lists_blacklisted_authors(self):
        self.client.force_login(self.superuser)

        response = self.client.get(
            self.admin_url("authors_author_blacklist"),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(
                response.context["cl"].queryset.values_list(
                    "pk", flat=True
                )
            ),
            [self.blacklisted_author.pk],
        )

    def test_superuser_can_remove_author_from_blacklist(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.admin_url(
                "authors_author_change",
                self.blacklisted_author.pk,
            ),
            {
                "first_name": self.blacklisted_author.first_name,
                "last_name": self.blacklisted_author.last_name,
                "pseudonym": "",
                "email": self.blacklisted_author.email,
                "contact": "on",
                "notes-TOTAL_FORMS": "0",
                "notes-INITIAL_FORMS": "0",
                "notes-MIN_NUM_FORMS": "0",
                "notes-MAX_NUM_FORMS": "1000",
                "_save": "Zapisz",
            },
        )

        self.assertEqual(response.status_code, 302)

        self.blacklisted_author.refresh_from_db()
        self.assertFalse(self.blacklisted_author.is_blacklisted)

    def test_new_note_creator_cannot_be_forged(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.admin_url("authors_authornote_add"),
            {
                "author": self.author.pk,
                "content": "Nowa notatka administratora.",
                "created_by": self.coordinator.pk,
                "_save": "Zapisz",
            },
        )

        self.assertEqual(response.status_code, 302)

        note = AuthorNote.objects.get(
            content="Nowa notatka administratora."
        )

        self.assertEqual(note.created_by_id, self.superuser.pk)

    def test_editing_note_preserves_original_creator(self):
        other_superuser = User.objects.create_superuser(
            username="drugi_superadmin",
            email="drugi@example.com",
            password="Inne-testowe-haslo-123!",
        )
        self.client.force_login(other_superuser)

        response = self.client.post(
            self.admin_url("authors_authornote_change", self.note.pk),
            {
                "author": self.author.pk,
                "content": "Zmieniona treść.",
                "created_by": other_superuser.pk,
                "_save": "Zapisz",
            },
        )

        self.assertEqual(response.status_code, 302)

        self.note.refresh_from_db()
        self.assertEqual(self.note.content, "Zmieniona treść.")
        self.assertEqual(self.note.created_by_id, self.superuser.pk)

    def test_editing_legacy_note_does_not_assign_a_new_creator(self):
        self.note.created_by = None
        self.note.save(update_fields=["created_by"])
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.admin_url("authors_authornote_change", self.note.pk),
            {
                "author": self.author.pk,
                "content": "Uzupełniona stara notatka.",
                "_save": "Zapisz",
            },
        )

        self.assertEqual(response.status_code, 302)

        self.note.refresh_from_db()
        self.assertIsNone(self.note.created_by_id)

    def test_new_inline_note_records_acting_superuser(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.admin_url(
                "authors_author_change",
                self.blacklisted_author.pk,
            ),
            {
                "first_name": self.blacklisted_author.first_name,
                "last_name": self.blacklisted_author.last_name,
                "pseudonym": "",
                "email": self.blacklisted_author.email,
                "contact": "on",
                "is_blacklisted": "on",
                "notes-TOTAL_FORMS": "1",
                "notes-INITIAL_FORMS": "0",
                "notes-MIN_NUM_FORMS": "0",
                "notes-MAX_NUM_FORMS": "1000",
                "notes-0-author": self.blacklisted_author.pk,
                "notes-0-content": "Notatka z formularza autora.",
                "notes-0-created_by": self.coordinator.pk,
                "_save": "Zapisz",
            },
        )

        self.assertEqual(response.status_code, 302)

        note = AuthorNote.objects.get(
            author=self.blacklisted_author,
            content="Notatka z formularza autora.",
        )

        self.assertEqual(note.created_by_id, self.superuser.pk)
