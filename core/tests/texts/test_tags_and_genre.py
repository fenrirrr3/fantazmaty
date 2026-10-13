"""Edycja tagów i gatunku tekstu na stronie tekstu: uprawnienia, normalizacja, konflikty wersji,
bezpieczeństwo i niezależność od gatunku recenzji."""
from django.test import Client, TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from fantazmaty.testing import create_member
from illustrations.models import Illustration
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowRoleAssignment

from .support import TAG_FORM, TagFormMixin, TaggedTextsFixture, source_review


class TextTagsEditingTests(TaggedTextsFixture, TestCase):
    def post(self, value):
        return self.client.post(self.save, {"tags": value, "_edit_version": self.tag_form_token()})

    def test_only_assigned_person_or_coordinator_can_edit(self):
        """Tagi i gatunek zmienia osoba z przydziałem do bieżącego cyklu tekstu albo koordynator;
        rola zespołu bez przydziału (Recenzent, Ilustrator) dostaje 403 i nie widzi formularza."""
        reviewer = create_member("tags-reviewer", "Recenzent")
        self.client.force_login(reviewer)
        response = self.client.get(self.detail)
        self.assertFalse(html.fromstring(response.content).xpath(TAG_FORM.rstrip('/')))
        self.assertContains(response, "Tagi i gatunek może zmienić osoba przypisana do tekstu lub koordynator.")
        token = self.tag_form_token_for(self.coordinator)
        for user in (reviewer, self.artist):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(self.save, {"tags": "cudze", "_edit_version": token})
                self.assertEqual(response.status_code, 403)
                self.text.refresh_from_db()
                self.assertEqual(self.text.tags, "magia, podróż")
        for user in (self.member, self.coordinator, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.post("tag " + str(user.pk)).status_code, 302)
                self.text.refresh_from_db()
                self.assertEqual(self.text.tags, "tag " + str(user.pk))

    def test_account_preview_cannot_write_tags(self):
        from core.user_preview import SESSION_KEY

        self.client.force_login(self.admin)
        session = self.client.session
        session[SESSION_KEY] = {"actor": self.admin.pk, "target": self.member.pk}
        session.save()
        self.assertEqual(self.client.post(self.save, {"tags": "Preview"}).status_code, 403)

    def test_tags_normalize_duplicates_whitespace_and_empty_input_clears(self):
        self.assertEqual(
            self.post("  Magia, magia\r\npodróż,  daleki   kosmos, PODRÓŻ,, ").status_code, 302
        )
        self.text.refresh_from_db()
        self.assertEqual(self.text.tags, "Magia, podróż, daleki kosmos")
        self.assertContains(self.client.get(self.list), "Magia, podróż, daleki kosmos")
        self.assertEqual(self.post(" ,\n ").status_code, 302)
        self.text.refresh_from_db()
        self.assertEqual(self.text.tags, "")

    def test_oversized_input_does_not_save_and_preserves_input(self):
        response = self.post("a" * 5001)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "a" * 5001, status_code=400)
        self.text.refresh_from_db()
        self.assertEqual(self.text.tags, "magia, podróż")

    def test_stale_or_missing_version_is_rejected_and_input_recoverable(self):
        """Stary formularz nie nadpisuje nowszych tagów ani gatunku; formularz bez pola gatunku
        (sama lista tagów) nie czyści gatunku."""
        old = self.tag_form_token()
        self.post("Nowszy tag")
        self.text.refresh_from_db()
        self.assertEqual(self.text.genre, "Fantasy")
        response = self.client.post(
            self.save, {"tags": "Stary formularz", "genre": "old", "_edit_version": old}
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Stary formularz", status_code=409)
        self.text.refresh_from_db()
        self.assertEqual((self.text.tags, self.text.genre), ("Nowszy tag", "Fantasy"))
        self.assertEqual(self.client.post(self.save, {"tags": "Brak tokena"}).status_code, 409)

    def test_csrf_get_and_nonexistent_text_are_handled(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.member)
        self.assertEqual(csrf.post(self.save, {"tags": "No"}).status_code, 403)
        self.assertEqual(self.client.get(self.save).status_code, 405)
        missing = reverse("core:update_text_tags", args=[999999])
        self.assertEqual(self.client.post(missing, {"tags": "No"}).status_code, 404)

    def test_tag_and_genre_html_is_escaped_and_genre_trimmed(self):
        self.client.post(self.save, {
            "tags": "<script>alert(1)</script>",
            "genre": "  <b>groza</b>  ",
            "_edit_version": self.tag_form_token(),
        })
        self.text.refresh_from_db()
        self.assertEqual(self.text.genre, "<b>groza</b>")
        for url in (self.list, self.detail):
            response = self.client.get(url)
            self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
            self.assertNotContains(response, "<script>alert(1)</script>")
        response = self.client.get(self.list)
        self.assertContains(response, "&lt;b&gt;groza&lt;/b&gt;")
        self.assertNotContains(response, "<b>groza</b>")

    def test_only_tag_field_changes_and_workflow_is_untouched(self):
        fields = ("title", "anthology_id", "content_warnings", "coordinator_note")
        before = tuple(getattr(self.text, name) for name in fields)
        stages = list(self.text.workflow_stages.values())
        response = self.client.post(self.save, {
            "tags": "nowy",
            "title": "Nadpisany",
            "coordinator_note": "Nadpisana",
            "_edit_version": self.tag_form_token(),
        })
        self.assertEqual(response.status_code, 302)
        self.text.refresh_from_db()
        self.assertEqual(tuple(getattr(self.text, name) for name in fields), before)
        self.assertEqual(list(self.text.workflow_stages.values()), stages)

    def test_warnings_tags_and_genre_share_one_grid_and_link_to_tag_list(self):
        """Ostrzeżenia, tagi i gatunek to trzy panele jednej siatki – także w widoku tylko do odczytu;
        przy edycji tagi i gatunek zapisuje jeden formularz rozpięty na dwie kolumny."""
        reviewer = create_member("grid-reviewer", "Recenzent")
        for user, editable in ((self.member, True), (reviewer, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.detail)
                grid = html.fromstring(response.content).xpath('//div[@data-text-info-grid]')[0]
                self.assertEqual(grid.get("class"), "text-info-grid")
                panels = grid.xpath('./div[contains(@class,"notes-editor-panel")]'
                                    ' | ./form/div[contains(@class,"notes-editor-panel")]')
                self.assertEqual([" ".join(panel.xpath("./h3")[0].text_content().split()) for panel in panels],
                                 ["Ostrzeżenia dotyczące treści", "Tagi", "Gatunek"])
                forms = grid.xpath('./form[contains(@class,"text-tags-form")]')
                self.assertEqual(len(forms), int(editable))
                if editable:
                    self.assertEqual(forms[0].xpath(".//textarea/@name"), ["tags", "genre"])
                    self.assertEqual(len(forms[0].xpath('.//button[@type="submit"]')), 1)
                self.assertContains(response, 'href="' + self.list + '"')
                self.assertContains(response, "core/tags.")

    def test_existing_rows_start_with_empty_tags_without_importing_genre(self):
        self.assertEqual(self.other.tags, "")
        self.assertEqual(self.orphan.tags, "")
        self.assertEqual(Text.objects.get(pk=self.text.pk).tags, "magia, podróż")


class TextGenreEditingTests(TagFormMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_member("genre-member", "Recenzent")
        cls.book = Anthology.objects.create(title="Antologia", has_illustrations=True)
        cls.text = Text.objects.create(
            title="Dawny tekst", anthology=cls.book, length=100, tags="magia"
        )
        WorkflowRoleAssignment.objects.create(text=cls.text, role="editor", assigned_to=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        self.detail = reverse("core:assigned_text_detail", args=[self.text.pk])
        self.save_url = reverse("core:update_text_tags", args=[self.text.pk])

    def post(self, **values):
        data = {"tags": "magia, las", "genre": "groza", **values}
        return self.client.post(self.save_url, {**data, "_edit_version": self.tag_form_token()})

    def test_member_saves_tags_and_genre_without_review_and_keeps_review_genre(self):
        version = version_of(self.text)
        self.assertEqual(self.post().status_code, 302)
        self.text.refresh_from_db()
        self.assertEqual((self.text.tags, self.text.genre), ("magia, las", "groza"))
        self.assertEqual(version_of(self.text), version + 1)
        self.assertFalse(Review.objects.exists())
        doc = html.fromstring(self.client.get(self.detail).content)
        self.assertEqual(
            doc.xpath('//dt[text()="Gatunek"]/following-sibling::dd/text()'), ["groza"]
        )
        review = source_review(self.text, "Fantasy")
        self.assertEqual(self.post(genre="horror").status_code, 302)
        review.refresh_from_db()
        self.assertEqual(review.genre, "Fantasy")

    def test_illustration_uses_text_genre_even_when_cleared_despite_review(self):
        source_review(self.text, "Fantasy")
        self.text.genre = "Groza"
        self.text.save(update_fields=["genre"])
        self.assertEqual(Illustration.objects.get(text=self.text).genre_tags_display, "Groza · magia")
        self.assertEqual(self.post(tags="magia", genre="").status_code, 302)
        self.text.refresh_from_db()
        self.assertEqual(self.text.genre, "")
        self.assertEqual(Illustration.objects.get(text=self.text).genre_tags_display, "magia")

    def test_oversized_genre_rolls_back_tags_and_displays_error(self):
        response = self.post(tags="changed", genre="g" * 101)
        self.assertEqual(response.status_code, 400)
        self.text.refresh_from_db()
        self.assertEqual((self.text.tags, self.text.genre), ("magia", ""))

    def test_admin_prefill_preserves_source_genre(self):
        from texts.admin import review_source_signature, review_text_initial

        review = source_review(self.text, "Fantasy")
        self.assertEqual(review_text_initial(review)["genre"], "Fantasy")
        signature = review_source_signature(review)
        review.genre = "Groza"
        self.assertNotEqual(signature, review_source_signature(review))
