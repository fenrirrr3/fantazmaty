"""Regresje: normalizacja, import, widoczność historii i filtry etapów."""
from datetime import timedelta

from django.contrib import admin
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.admin import AuthorAdminForm
from authors.models import Author, BlacklistedAuthor
from texts.admin import ReviewAdminForm, TextAdminForm
from texts.models import Review, Text, TextNote
from workflow.models import WorkflowStage
from .forms import (
    AuthorNotificationForm, ReviewBulkImportForm, ReviewerOpinionForm,
    ReviewContentWarningsForm, TextContentWarningsForm,
)
from .selectors.texts import text_detail_context, workflow_list_context
from .services.reviews import import_reviews
from .tests import CoreTestDataMixin


class InputNormalizationTests(CoreTestDataMixin, TestCase):
    def test_author_model_normalizes_before_validation_and_save(self):
        author = Author(first_name="  jan   adam ", last_name=" żółć ",
                        email=" TEST@Example.COM ")
        author.full_clean()
        author.save()
        author.refresh_from_db()
        self.assertEqual((author.first_name, author.last_name, author.email),
                         ("Jan Adam", "Żółć", "test@example.com"))

    def test_author_form_normalizes_email_before_duplicate_validation(self):
        form = AuthorAdminForm(data={"first_name": "jan", "last_name": "test",
                                     "email": self.author.email.upper()})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_author_form_rejects_internal_email_spaces(self):
        form = AuthorAdminForm(data={"first_name": "jan", "last_name": "test",
                                     "email": "wrong @example.com"})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_text_admin_form_normalizes_title_and_warnings(self):
        form = TextAdminForm(data={"title": "  Tytuł   tekstu  ", "length": 100,
                                  "authors": [self.author.pk],
                                  "content_warnings": "  PRZEMOC   GROZA  "})
        self.assertTrue(form.is_valid(), form.errors)
        text = form.save()
        self.assertEqual(text.title, "Tytuł tekstu")
        self.assertEqual(text.content_warnings, "przemoc groza")

    def test_review_admin_form_normalizes_all_input(self):
        form = ReviewAdminForm(data={
            "author_first_name": "  jan  ", "author_last_name": "  kowalski  ",
            "email": " NEW@Example.COM ", "title": " Nowy   tytuł ",
            "genre": " urban   fantasy ", "length": 123,
            "anthology": self.anthology.pk, "status": "new",
            "content_warnings": " PRZEMOC  ", "phone_number": " 123   456 ",
        })
        self.assertTrue(form.is_valid(), form.errors)
        review = form.save()
        self.assertEqual(review.author_first_name, "Jan")
        self.assertEqual(review.author_last_name, "Kowalski")
        self.assertEqual(review.email, "new@example.com")
        self.assertEqual(review.content_warnings, "przemoc")
        self.assertEqual(review.phone_number, "123 456")

    def test_warnings_edit_forms_normalize(self):
        for form_class in (TextContentWarningsForm, ReviewContentWarningsForm,
                           ReviewerOpinionForm):
            form = form_class(data={"content_warnings": " GROZA  PRZEMOC ",
                                    "opinion": "yes", "notes": "Recenzja"})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["content_warnings"], "groza przemoc")

    def test_partial_save_does_not_rewrite_unrelated_fields(self):
        text = Text.objects.create(title="Pierwotny", length=100)
        text.title = "Nie zapisuj"
        text.content_warnings = " GROZA  "
        text.save(update_fields=["content_warnings"])
        text.refresh_from_db()
        self.assertEqual(text.title, "Pierwotny")
        self.assertEqual(text.content_warnings, "groza")

    def import_form(self, record):
        return ReviewBulkImportForm({"anthology": self.anthology.pk,
                                     "records": record}, user=self.superuser)

    def test_plain_and_legacy_import_parse_equally(self):
        fields = [" jan   kowalski ", " Test   importu ", "fantasy", "2 500",
                  " PRZEMOC  GROZA ", " NEW@Example.COM ", " 123   456 "]
        plain = self.import_form(";".join(fields) + "  \n")
        legacy = self.import_form(";".join(f"[{value}]" for value in fields))
        self.assertTrue(plain.is_valid(), plain.errors)
        self.assertTrue(legacy.is_valid(), legacy.errors)
        self.assertEqual(plain.parsed_records, legacy.parsed_records)
        self.assertEqual(plain.parsed_records[0]["author_name"], "JAN KOWALSKI")
        self.assertEqual(plain.parsed_records[0]["content_warnings"], "przemoc groza")

    def test_import_rejects_wrong_field_count(self):
        for line in ("Jan Kowalski;Tytuł", "Jan Kowalski;t;f;123;;e@x.pl;123;extra"):
            form = self.import_form(line)
            self.assertFalse(form.is_valid())
            self.assertIn("records", form.errors)

    def test_plain_import_saves_normalized_records(self):
        form = self.import_form("jan kowalski; Tytuł   testowy ;fantasy;123;GROZA;NEW@example.com;123")
        self.assertTrue(form.is_valid(), form.errors)
        import_reviews(user=self.superuser, form=form)
        review = Review.objects.get()
        self.assertEqual(review.author_first_name, "Jan")
        self.assertEqual(review.email, "new@example.com")
        self.assertEqual(review.title, "Tytuł testowy")
        self.assertEqual(review.content_warnings, "groza")

    def test_notification_date_defaults_to_today(self):
        form = AuthorNotificationForm()
        self.assertEqual(form.initial["author_notified_at"], timezone.localdate())
        form = AuthorNotificationForm(data={"author_notified": "on"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["author_notified_at"], timezone.localdate())

    def test_notification_preserves_existing_date_and_allows_uncheck(self):
        old_date = timezone.localdate() - timedelta(days=7)
        review = Review(author_notified_at=old_date)
        form = AuthorNotificationForm(review=review)
        self.assertEqual(form.initial["author_notified_at"], old_date)
        form = AuthorNotificationForm(data={"author_notified_at": old_date})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["author_notified_at"])


class InterfaceAccessTests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.text = Text.objects.create(title="Test historii", length=100,
                                        anthology=self.anthology, current_workflow_cycle=2)
        self.text.authors.add(self.author)
        self.past = WorkflowStage.objects.create(text=self.text, workflow_cycle=1,
                                                stage_type="editing", iteration=3)
        self.current = WorkflowStage.objects.create(text=self.text, workflow_cycle=2,
                                                   stage_type="first_proofreading")

    def test_stage_history_only_for_coordinator_or_superuser(self):
        for user, allowed in [(self.superuser, True), (self.coordinator, True),
                              (self.editor, False), (self.reviewer, False)]:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["can_view_stage_history"], allowed)
                if allowed:
                    self.assertContains(response, "Historia etapów")
                    self.assertIn(self.past.pk, [row["pk"] for row in response.context["archived_stages"]])
                else:
                    self.assertNotContains(response, "Historia etapów")
                    self.assertEqual(response.context["archived_stages"], [])
                self.assertNotContains(response, "Iteracja")

    def test_empty_coordinator_note_hidden_but_coordinator_can_add(self):
        self.text.coordinator_note = "   "
        self.text.save(update_fields=["coordinator_note"])
        for user in (self.editor, self.coordinator):
            self.client.force_login(user)
            response = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
            self.assertFalse(response.context["show_coordinator_note"])
            self.assertNotContains(response, 'class="notes-editor-panel coordinator-note-highlight"')
            if user == self.coordinator:
                self.assertContains(response, "Dodaj notatkę koordynatora")
            else:
                self.assertNotContains(response, "Dodaj notatkę koordynatora")

    def test_existing_and_important_notes_have_highlight_classes(self):
        self.text.coordinator_note = "Uwaga koordynatora"
        self.text.save(update_fields=["coordinator_note"])
        TextNote.objects.create(text=self.text, author=self.coordinator,
                                content="Ważna treść", is_important=True)
        self.client.force_login(self.editor)
        response = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
        self.assertContains(response, 'class="notes-editor-panel coordinator-note-highlight"')
        self.assertContains(response, 'class="text-note is-important"')

    def test_multi_stage_filter_uses_union_and_keeps_anthology_filter(self):
        WorkflowStage.objects.create(text=self.text, workflow_cycle=2, stage_type="styling")
        WorkflowStage.objects.create(text=self.text, workflow_cycle=2, stage_type="ready")
        params = QueryDict(f"stage=first_proofreading&stage=styling&stage=invalid&anthology={self.anthology.pk}")
        context = workflow_list_context(user=self.coordinator, params=params)
        self.assertEqual({row["stage_type"] for row in context["stages"]},
                         {"first_proofreading", "styling"})
        self.client.force_login(self.coordinator)
        response = self.client.get(reverse("core:workflow_list") + "?" + params.urlencode())
        self.assertContains(response, 'type="checkbox" name="stage"')
        self.assertEqual(response.context["selected_stages"], ["first_proofreading", "styling"])
        self.assertIn("stage=styling", response.context["page_obj"].query_string)
        self.assertNotContains(response, "iteracja")

    def test_multi_anthology_filter(self):
        from texts.models import Anthology
        second = Anthology.objects.create(title="Druga")
        third = Anthology.objects.create(title="Trzecia")
        for anthology in (second, third):
            text = Text.objects.create(title=anthology.title, anthology=anthology, length=123)
            WorkflowStage.objects.create(text=text, stage_type="styling")
        params = QueryDict(f"anthology={self.anthology.pk}&anthology={second.pk}")
        context = workflow_list_context(user=self.coordinator, params=params)
        self.assertEqual(len(list(context["stages"])), 2)

    def test_author_list_compact_booleans_and_no_email_copy_button(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("core:author_list"))
        self.assertContains(response, 'class="boolean-icon boolean-yes"')
        self.assertContains(response, 'class="boolean-icon boolean-no"')
        self.assertNotContains(response, 'class="copy-value-button"')

    def test_login_has_smaller_centered_reset_link(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'class="secondary-button auth-password-reset"')


class BlacklistAdminTests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.blacklisted = Author.objects.create(first_name="Zły", last_name="Autor",
                                                email="black@example.com", is_blacklisted=True)

    def test_proxy_uses_same_author_table(self):
        self.assertEqual(Author._meta.db_table, BlacklistedAuthor._meta.db_table)
        self.assertIn(BlacklistedAuthor, admin.site._registry)

    def test_blacklist_list_filters_even_when_query_requests_nonblacklisted(self):
        self.client.force_login(self.superuser)
        url = reverse("admin:authors_blacklistedauthor_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["cl"].queryset.values_list("pk", flat=True)),
                         [self.blacklisted.pk])
        response = self.client.get(url, {"is_blacklisted__exact": "0"})
        self.assertNotContains(response, self.author.email)

    def test_blacklist_edit_form_and_distinct_boolean_badge(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:authors_blacklistedauthor_change", args=[self.blacklisted.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="is_blacklisted"')
        response = self.client.get(reverse("admin:authors_author_changelist"))
        self.assertContains(response, "✓ TAK")
        self.assertContains(response, "✗ NIE")

    def test_coordinator_cannot_read_admin_blacklist(self):
        self.coordinator.is_staff = True
        self.coordinator.save(update_fields=["is_staff"])
        self.client.force_login(self.coordinator)
        response = self.client.get(reverse("admin:authors_blacklistedauthor_changelist"))
        self.assertEqual(response.status_code, 403)
