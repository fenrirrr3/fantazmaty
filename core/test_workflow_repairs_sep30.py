from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.urls import reverse
from people.models import Person, Role
from texts.models import Text, Review, Anthology
from authors.models import Author
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.import_context import importing_completed
from workflow.read_queries import available_stages, dashboard_querysets
from workflow.availability import claim_access
from workflow.services import claim_stage
from workflow.admin_add_stage import add_missing_stage
from core.edit_versions import version_of
from core.selectors.texts import workflow_list_context
from core.source_reviews import link_source_review
from core.services.reviews import copy_review_to_text
import time

class WorkflowRepairs(TestCase):
    def setUp(self):
        self.admin=get_user_model().objects.create_superuser('admin','admin@example.com','test')
        self.user=get_user_model().objects.create_user('user',email='u@example.com')
        p=Person.objects.create(user=self.user,first_name='Jan',last_name='Osoba',email='u@example.com',is_active=True)
        for name in ['Redaktor','Weryfikator']:
            role, _ = Role.objects.get_or_create(name=name)
            p.roles.add(role)
        self.book = Anthology.objects.create(title="Nabór")
        self.text = Text.objects.create(title="Tytuł", length=100, anthology=self.book)
        self.today = timezone.localdate()
        self.client.force_login(self.admin)

    def stage(self, kind, role=None, **kw):
        a = A.objects.create(text=self.text, role=role, assigned_to=self.user) if role else None
        return S.objects.create(text=self.text, stage_type=kind, assignment=a, **kw)

    def review(self, **kw):
        data = dict(
            title="Tytuł",
            anthology=self.book,
            author_first_name="Jan",
            author_last_name="Autor",
            email="autor@example.com",
            length=100,
            genre="fantasy",
            status="accepted",
            author_notified_at=self.today,
        )
        data.update(kw)
        return Review.objects.create(**data)

    def test_undated_import_available_and_immediate_start(self):
        token = importing_completed.set(True)
        try:
            self.stage("editing", "editor", is_completed=True, imported_completed=True)
        finally:
            importing_completed.reset(token)
        v = self.stage("first_verification")
        self.assertTrue(
            available_stages(self.user, claim_access(self.user)).filter(pk=v.pk).exists()
        )
        claim_stage(self.text, "first_verification", self.user)
        v.refresh_from_db()
        self.assertEqual(v.started_at, self.today)

    def test_w1_reservation_during_editing(self):
        self.stage("editing", "editor", started_at=self.today)
        v = self.stage("first_verification")
        claim_stage(self.text, "first_verification", self.user)
        v.refresh_from_db()
        self.assertIsNone(v.started_at)

    def test_author_visible_on_dashboard(self):
        a = A.objects.create(text=self.text, role="editor", assigned_to=self.user)
        S.objects.create(
            text=self.text,
            stage_type="editing",
            assignment=a,
            is_completed=True,
            started_at=self.today,
            ended_at=self.today,
        )
        s = S.objects.create(
            text=self.text, stage_type="author_editing", assignment=a, started_at=self.today
        )
        active, reserved = dashboard_querysets(self.user, self.today)
        self.assertTrue(active.filter(pk=s.pk).exists())
        self.assertFalse(reserved.filter(text=self.text).exists())

    def test_summary_waits_for_editorial_approval(self):
        self.stage(
            "editing", "editor", is_completed=True, started_at=self.today, ended_at=self.today
        )
        self.stage("first_verification")

        def state():
            return list(workflow_list_context(user=self.admin, params={})["stages"])[0][
                "role_cells"
            ][0]["entries"][0]["state"]

        self.assertEqual(state(), "Oczekuje")
        self.stage("editing_control", is_completed=True, started_at=self.today, ended_at=self.today)
        self.assertEqual(state(), "Oczekuje")
        proof = self.stage("first_proofreading")
        self.assertEqual(state(), "Zakończone")
        proof.started_at = self.today
        proof.save()
        self.assertEqual(state(), "Zakończone")

    def test_mismatch_and_detached_republication(self):
        a = Author.objects.create(
            first_name="Jan", last_name="Autor", email="autor@example.com", has_contract=True
        )
        self.text.authors.add(a)
        old = self.review(author=a)
        new = self.review(
            title="Inny tytuł",
            author_first_name="Inna",
            author_last_name="Osoba",
            email="inna@example.com",
        )
        link_source_review(user=self.admin, text_id=self.text.pk, review_id=old.pk)
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=new.pk)
        old.refresh_from_db()
        self.assertEqual(old.copied_text_id, self.text.pk)
        link_source_review(
            user=self.admin, text_id=self.text.pk, review_id=new.pk, confirm_mismatch=True
        )
        old.refresh_from_db()
        self.assertTrue(old.publication_detached)
        with self.assertRaises(ValidationError):
            copy_review_to_text(user=self.admin, review_id=old.pk)
        response = self.client.get(reverse("core:home"))
        self.assertNotIn(old.pk, [r.pk for r in response.context["pending_publication_reviews"]])
        link_source_review(user=self.admin, text_id=self.text.pk, review_id=old.pk)
        old.refresh_from_db()
        self.assertFalse(old.publication_detached)

    def test_history_without_dates_inactive_performer(self):
        self.stage("third_verification")
        self.user.is_active = False
        self.user.save()
        s = add_missing_stage(
            self.text.pk,
            self.admin,
            version_of(self.text),
            kind="editing",
            performer=self.user,
            historical=True,
        )
        self.assertTrue(s.is_completed)
        self.assertTrue(s.imported_completed)
        self.assertIsNone(s.started_at)
        self.assertIsNone(s.ended_at)
        self.assertIsNone(s.assignment.assigned_at)
        active, reserved = dashboard_querysets(self.user, self.today)
        self.assertFalse(active.filter(pk=s.pk).exists())
        self.assertFalse(reserved.filter(text=self.text).exists())

    def test_live_history_checkbox_required(self):
        with self.assertRaises(ValidationError):
            add_missing_stage(
                self.text.pk,
                self.admin,
                version_of(self.text),
                kind="editing",
                performer=self.user,
                ended_at=self.today,
            )

    def test_closed_anthology_not_pending_publication(self):
        self.stage("ready", is_completed=True, started_at=self.today, ended_at=self.today)
        self.text.current_status = "ready"
        self.text.save()
        self.book.status = "ready"
        self.book.save()
        review = self.review(title="Zablokowane zgłoszenie")
        self.assertNotContains(self.client.get(reverse("core:home")), review.title)

    def test_pseudonym_search(self):
        self.review(author_pseudonym="Nietypowypseudonim", title="Unikalna historia pseudonimu", status="new")
        for url in ["core:review_list", "core:global_search"]:
            response = self.client.get(
                reverse(url),
                {"q": "Nietypowypseudonim", "query": "Nietypowypseudonim", "status": "new"},
            )
            self.assertContains(response, "Unikalna historia pseudonimu")

    def test_admin_popup_resolves_new_author_with_contract(self):
        review = self.review(author_pseudonym="Pseudonim autora", phone_number="123456789")
        session = self.client.session
        session["review_text_prefills"] = {
            "token": {
                "user": self.admin.pk,
                "at": time.time(),
                "review_id": review.pk,
                "data": {
                    "title": review.title,
                    "length": 100,
                    "anthology": self.book.pk,
                    "source_author_first_name": review.author_first_name,
                    "source_author_last_name": review.author_last_name,
                    "source_author_email": review.email,
                    "source_author_pseudonym": review.author_pseudonym,
                },
            }
        }
        session.save()
        url = reverse("admin:texts_text_add") + "?_popup=1&prefill=token"
        initial = self.client.get(url)
        self.assertEqual(initial.status_code, 200)
        data = {
            "title": review.title,
            "length": 100,
            "anthology": self.book.pk,
            "source_author_first_name": review.author_first_name,
            "source_author_last_name": review.author_last_name,
            "source_author_email": review.email,
            "source_author_pseudonym": review.author_pseudonym,
            "source_contract_received": "on",
            "source_update_author_phone": "on",
            "_save": "Save",
        }
        for inline in initial.context["inline_admin_formsets"]:
            prefix = inline.formset.prefix
            data[prefix + "-TOTAL_FORMS"] = "0"
            data[prefix + "-INITIAL_FORMS"] = "0"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, response.content[:500])
        review.refresh_from_db()
        self.assertIsNotNone(
            review.copied_text_id, response.context and response.context["adminform"].form.errors
        )
        self.assertEqual(review.author.pseudonym, "Pseudonim autora")
        self.assertEqual(review.author.phone_number, "123456789")
        self.assertTrue(review.author.has_contract)

    def popup_data(self, review):
        session = self.client.session
        session["review_text_prefills"] = {
            "token": {
                "user": self.admin.pk,
                "at": time.time(),
                "review_id": review.pk,
                "data": {
                    "title": review.title,
                    "length": 100,
                    "anthology": self.book.pk,
                    "source_author_first_name": review.author_first_name,
                    "source_author_last_name": review.author_last_name,
                    "source_author_email": review.email,
                },
            }
        }
        session.save()
        url = reverse("admin:texts_text_add") + "?_popup=1&prefill=token"
        response = self.client.get(url)
        data = {
            key: value
            for key, value in response.context["adminform"].form.initial.items()
            if key != "source_review_id"
        }
        data["_save"] = "Save"
        for inline in response.context["inline_admin_formsets"]:
            data[inline.formset.prefix + "-TOTAL_FORMS"] = "0"
            data[inline.formset.prefix + "-INITIAL_FORMS"] = "0"
        return url, data, response

    def test_popup_requires_contract_and_rolls_back(self):
        review = self.review()
        url, data, _ = self.popup_data(review)
        count = Text.objects.count()
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["adminform"].form.non_field_errors())
        self.assertEqual(Text.objects.count(), count)
        self.assertFalse(Author.objects.filter(email=review.email).exists())
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)

    def test_popup_confirms_coauthor_contracts(self):
        review = self.review()
        coauthor = Author.objects.create(
            first_name="Anna", last_name="Druga", email="druga@example.com"
        )
        review.coauthors.add(coauthor)
        url, data, response = self.popup_data(review)
        self.assertIn(
            coauthor,
            response.context["adminform"].form.fields["source_coauthor_contracts"].queryset,
        )
        data["source_contract_received"] = "on"
        data["source_coauthor_contracts"] = [str(coauthor.pk)]
        response = self.client.post(url, data)
        self.assertIn(response.status_code, (200, 302))
        review.refresh_from_db()
        coauthor.refresh_from_db()
        self.assertTrue(coauthor.has_contract)
        self.assertEqual(
            set(review.copied_text.authors.values_list("pk", flat=True)),
            {review.author_id, coauthor.pk},
        )

    def test_normal_text_admin_has_no_source_confirmations(self):
        response = self.client.get(reverse("admin:texts_text_add"))
        self.assertNotContains(response, "id_source_contract_received")

    def test_same_names_different_profiles_require_confirmation(self):
        author = Author.objects.create(
            first_name="Jan", last_name="Autor", email="autor@example.com"
        )
        other = Author.objects.create(first_name="Jan", last_name="Autor", email="inny@example.com")
        self.text.authors.add(author)
        review = self.review(author=other)
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=review.pk)

    def test_direct_popup_uses_publication_gates(self):
        review = self.review()
        response = self.client.get(
            reverse("admin:texts_text_add"), {"source_review": review.pk, "_popup": 1}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id_source_contract_received")
        review.status = "rejected"
        review.save()
        response = self.client.get(
            reverse("admin:texts_text_add"), {"source_review": review.pk, "_popup": 1}
        )
        self.assertEqual(response.status_code, 400)

    def admin_link_form(self, review, text, **overrides):
        from texts.admin import ReviewAdminForm

        data = {
            name: getattr(review, name)
            for name in (
                "title",
                "genre",
                "length",
                "content_warnings",
                "email",
                "phone_number",
                "author_first_name",
                "author_last_name",
                "author_pseudonym",
                "status",
            )
        }
        data.update(
            anthology=review.anthology_id,
            author=review.author_id or "",
            copied_text=text.pk,
            confirm_existing_text="on",
            old_reviews="on",
        )
        data.update(overrides)
        return ReviewAdminForm(data=data, instance=review)

    def test_admin_allows_cross_anthology_but_rejects_unaccepted_source(self):
        author = Author.objects.create(
            first_name="Jan", last_name="Autor", email="autor@example.com"
        )
        self.text.authors.add(author)
        self.stage("editing")
        other = Anthology.objects.create(title="Inny nabór")
        review = self.review(author=author, old_reviews=False, anthology=other)
        form = self.admin_link_form(review, self.text, confirm_source_mismatch="on", old_reviews="")
        self.assertTrue(form.is_valid(), form.errors)
        for changes in ({"status": "new"}, {"status": "in_review"}):
            review = self.review(author=author, old_reviews=False, **changes)
            form = self.admin_link_form(review, self.text, confirm_source_mismatch="on", old_reviews="")
            self.assertFalse(form.is_valid())
            self.assertIn("copied_text", form.errors)
            self.assertIn("przyjęte lub archiwalne zgłoszenie", str(form.errors["copied_text"]))

    def test_admin_requires_separate_confirmation_of_author_mismatch(self):
        author = Author.objects.create(
            first_name="Jan", last_name="Autor", email="autor@example.com"
        )
        other = Author.objects.create(
            first_name="Inna", last_name="Osoba", email="inna@example.com"
        )
        self.text.authors.add(author)
        self.stage("editing")
        review = self.review(author=other, old_reviews=True)
        form = self.admin_link_form(review, self.text)
        self.assertFalse(form.is_valid())
        self.assertIn("rozbieżności", str(form.errors["copied_text"]))
        form = self.admin_link_form(review, self.text, confirm_source_mismatch="on")
        form.is_valid()
        self.assertNotIn("copied_text", form.errors)

    def test_popup_authors_are_readonly_and_forged_selection_ignored(self):
        author = Author.objects.create(
            first_name="Jan", last_name="Autor", email="autor@example.com", has_contract=True
        )
        other = Author.objects.create(
            first_name="Inna", last_name="Osoba", email="inna@example.com", has_contract=True
        )
        review = self.review(author=author)
        url, data, response = self.popup_data(review)
        self.assertTrue(response.context["adminform"].form.fields["authors"].disabled)
        data["authors"] = [str(other.pk)]
        result = self.client.post(url, data)
        self.assertIn(result.status_code, (200, 302))
        review.refresh_from_db()
        self.assertEqual(list(review.copied_text.authors.all()), [author])

    def test_popup_rejects_unsaved_data_and_stale_snapshot(self):
        review = self.review()
        endpoint = reverse("admin:texts_review_prepare_text")
        response = self.client.post(
            endpoint, {"review_id": review.pk, "title": "Niezapisany tytuł"}
        )
        self.assertEqual(response.status_code, 409)
        response = self.client.post(endpoint, {"review_id": review.pk})
        self.assertEqual(response.status_code, 200)
        url = response.json()["url"]
        response = self.client.get(url)
        data = {
            k: v
            for k, v in response.context["adminform"].form.initial.items()
            if k != "source_review_id"
        }
        data.update(source_contract_received="on", _save="Save")
        for inline in response.context["inline_admin_formsets"]:
            data[inline.formset.prefix + "-TOTAL_FORMS"] = "0"
            data[inline.formset.prefix + "-INITIAL_FORMS"] = "0"
        review.title = "Zapisany później tytuł"
        review.save()
        result = self.client.post(url, data)
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "Recenzja zmieniła się")
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)
        self.assertFalse(Author.objects.filter(email=review.email).exists())

    def test_popup_missing_contract_keeps_entered_text(self):
        review = self.review()
        url, data, _ = self.popup_data(review)
        data["title"] = "Tytuł do zachowania"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["adminform"].form["title"].value(), "Tytuł do zachowania")
        self.assertTrue(response.context["adminform"].form.non_field_errors())

    def test_history_does_not_erase_real_assignment_date(self):
        self.stage("third_verification")
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.user)
        known_date = assignment.assigned_at
        stage = add_missing_stage(
            self.text.pk,
            self.admin,
            version_of(self.text),
            kind="editing",
            performer=self.user,
            historical=True,
        )
        self.assertEqual(stage.assignment_id, assignment.pk)
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_at, known_date)

    def test_unlinked_candidates_are_manual_and_superuser_only(self):
        from core.supervision import unlinked_review_candidates

        review = self.review()
        self.review(title="Bez pasującego tekstu")
        self.review(publication_detached=True)
        self.assertEqual(list(unlinked_review_candidates()), [review])
        response = self.client.get(reverse("core:data_integrity"), {"tab": "unlinked"})
        self.assertContains(response, "Recenzje do ręcznej kontroli: 1")
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)
        self.assertFalse(review.publication_detached)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("core:data_integrity"), {"tab": "unlinked"}).status_code, 403
        )

    def test_mobile_sort_can_return_to_default(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        rendered = render_to_string(
            "core/includes/mobile_table_sort.html",
            {
                "request": RequestFactory().get("/moje-teksty/", {"sort": "title"}),
                "mobile_sort_columns": [("Tytuł", "title")],
            },
        )
        self.assertIn('<option value="">Kolejność domyślna</option>', rendered)
        self.assertIn('value="title" selected', rendered)

    def test_late_popup_error_rolls_back_and_renders_form(self):
        from unittest.mock import patch
        from texts.admin import TextAdmin

        review = self.review()
        url, data, _ = self.popup_data(review)
        data["source_contract_received"] = "on"
        count = Text.objects.count()
        original = TextAdmin.save_related

        def fail_after_save(admin, request, form, formsets, change):
            original(admin, request, form, formsets, change)
            raise ValidationError("Zapis został przerwany – sprawdź dane.")

        with patch.object(TextAdmin, "save_related", fail_after_save):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zapis został przerwany")
        self.assertEqual(Text.objects.count(), count)
        self.assertFalse(Author.objects.filter(email=review.email).exists())
        review.refresh_from_db()
        self.assertIsNone(review.author_id)
        self.assertIsNone(review.copied_text_id)
