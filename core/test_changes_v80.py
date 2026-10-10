from datetime import date

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.models import Audiobook, AudioContributor, PostLayoutAssignment
from core.post_layout import AssignmentEditForm
from core.source_reviews import link_source_review
from texts.extract_whole import ensure_whole_text
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.tests import create_member


class ChangesV80Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("v80-admin", "", "test")
        cls.reader = create_member("v80-reader", "Recenzent")
        cls.other = create_member("v80-other", "Recenzent")
        cls.editor = create_member("v80-editor", "Redaktor")
        cls.proofreader = create_member("v80-proofreader", "Korektor poskładowy")
        cls.book = Anthology.objects.create(title="Testowy tom")
        cls.text = Text.objects.create(title="Testowy tekst", anthology=cls.book, length=100)

    def setUp(self):
        self.client.force_login(self.admin)

    def token(self, text=None, user=None):
        text = text or self.text
        return signing.dumps(
            [(user or self.admin).pk, f"texts.text:{text.pk}", version_of(text)],
            salt="cms-edit-version",
        )

    def test_extract_panels_and_length_only_change_whole_volumes(self):
        book = Anthology.objects.create(title="Ekstrakty 3", is_extracts=True)
        whole = ensure_whole_text(book)
        page = self.client.get(reverse("core:assigned_text_detail", args=[whole.pk]))
        doc = html.fromstring(page.content)
        headings = [x.text_content().strip() for x in doc.xpath("//h2|//summary")]
        for label in [
            "Recenzje tekstu",
            "Audiobook",
            "Ilustracja",
            "Wycofaj tekst",
            "Tagi i gatunek",
        ]:
            self.assertFalse(any(label in value for value in headings), label)
        self.assertContains(page, "Notatki zespołu")
        self.assertContains(page, "nie dotyczy")
        page = self.client.get(reverse("core:text_list"), {"q": "Ekstrakty"})
        self.assertContains(page, "n.d.")
        self.assertNotContains(page, ">None<")
        normal = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
        self.assertContains(normal, "Recenzje tekstu")
        self.assertContains(normal, "Tagi i gatunek")

    def test_search_includes_external_inactive_and_independent_pages(self):
        external = create_member("external-needle", "Redaktor").person_profile
        external.first_name = "NeedleExternal"
        external.is_external = True
        external.save()
        inactive = create_member("inactive-needle", "Redaktor").person_profile
        inactive.first_name = "NeedleInactive"
        inactive.is_active = False
        inactive.save()
        for i in range(18):
            Text.objects.create(title=f"Needle {i:03}", anthology=self.book, length=1)
            AudioContributor.objects.create(name=f"NeedleAudio {i:03}")
        url = reverse("core:global_search")
        page = self.client.get(url, {"q": "Needle"})
        self.assertEqual(len(page.context["texts"]), 15)
        self.assertEqual(page.context["texts"].total, 18)
        self.assertEqual({p["pk"] for p in page.context["people"]}, {external.pk, inactive.pk})
        self.assertContains(page, "Zewnętrzny")
        self.assertContains(page, "Nieaktywny")
        self.assertEqual(
            self.client.get(reverse("core:person_detail", args=[inactive.pk])).status_code, 200
        )
        next_url = page.context["texts"].next_url
        self.assertIn("texts_page=2", next_url)
        self.assertIn("q=Needle", next_url)
        next_page = self.client.get(url + next_url)
        self.assertEqual(len(next_page.context["texts"]), 3)
        self.assertEqual(len(next_page.context["additional_results"][0]["items"]), 15)
        self.assertEqual(next_page.context["additional_results"][0]["items"].page.number, 1)
        page = self.client.get(url, {"q": "Needle", "audio_page": 2})
        self.assertEqual(len(page.context["additional_results"][0]["items"]), 3)
        self.assertEqual(page.context["texts"].page.number, 1)
        self.assertEqual(
            self.client.get(url, {"q": "Needle", "texts_page": "oops"}).status_code, 200
        )
        self.client.force_login(self.editor)
        self.assertEqual(
            len(self.client.get(url, {"q": "Needle"}).context["additional_results"]), 1
        )

    def historical(self):
        return PostLayoutAssignment.objects.create(
            anthology=self.book,
            proofreader=self.proofreader,
            historical=True,
            status="completed",
            assigned_start=None,
            page_from=None,
            page_to=None,
        )

    def test_partial_historical_dates_save_and_bad_order_is_rejected(self):
        item = self.historical()
        data = {
            "proofreader": self.proofreader.pk,
            "page_from": "",
            "page_to": "",
            "assigned_start": "",
            "work_start": "",
            "completed_on": "2020-05-04",
        }
        form = AssignmentEditForm(data, instance=item)
        self.assertTrue(form.is_valid(), form.errors)
        item = form.save()
        item.full_clean()
        self.assertEqual(item.completed_on, date(2020, 5, 4))
        self.assertIsNone(item.work_start)
        # Same form is registered in the admin. Known endpoints must be ordered
        # even if the middle date is missing.
        form = AssignmentEditForm({**data, "assigned_start": "2021-01-01"}, instance=item)
        self.assertFalse(form.is_valid())
        with self.assertRaises(IntegrityError), transaction.atomic():
            PostLayoutAssignment.objects.filter(pk=item.pk).update(assigned_start=date(2021, 1, 1))

    def test_normal_post_layout_still_requires_dates(self):
        item = PostLayoutAssignment.objects.create(
            anthology=self.book,
            proofreader=self.proofreader,
            created_by=self.admin,
            page_from=1,
            page_to=2,
        )
        form = AssignmentEditForm(
            {
                "proofreader": self.proofreader.pk,
                "page_from": 1,
                "page_to": 2,
                "assigned_start": "",
            },
            instance=item,
        )
        self.assertFalse(form.is_valid())

    def test_disabled_audiobook_metadata_editable_but_stages_blocked(self):
        audio = Audiobook.objects.create(
            text=self.text, status="published", narrator_name="Pierwotny lektor"
        )
        self.text.audiobook_blacklisted = True
        self.text.save()
        url = reverse("core:audiobook_detail", args=[self.text.pk])
        page = self.client.get(url)
        self.assertTrue(page.context["can_edit"])
        self.assertFalse(page.context["can_manage_stages"])
        self.assertNotContains(page, "Dodaj etap")
        data = {
            "action": "publication",
            "_edit_version": self.token(),
            "publication-premiere_date": "2020-05-04",
            "publication-youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "publication-hearthis_url": "",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.premiere_date, date(2020, 5, 4))
        self.assertEqual(audio.status, "published")
        self.assertEqual(
            self.client.post(
                url,
                {
                    "action": "start_stage",
                    "_edit_version": self.token(),
                    "stage_type": "recording",
                    "started_at": "2020-05-05",
                },
            ).status_code,
            403,
        )
        self.client.force_login(self.editor)
        self.assertEqual(
            self.client.post(
                url, {**data, "_edit_version": self.token(user=self.editor)}
            ).status_code,
            403,
        )

    def test_disabled_text_without_audio_cannot_create_metadata(self):
        self.text.for_recording = False
        self.text.save()
        url = reverse("core:audiobook_detail", args=[self.text.pk])
        self.assertFalse(self.client.get(url).context["can_edit"])
        self.assertEqual(
            self.client.post(
                url,
                {
                    "action": "people",
                    "_edit_version": self.token(),
                    "people-narrator_name": "Nowy lektor",
                },
            ).status_code,
            403,
        )
        self.assertFalse(Audiobook.objects.exists())

    def test_unlink_requires_confirmation_preserves_history_and_prevents_duplicate(self):
        review = Review.objects.create(
            title=self.text.title,
            anthology=self.book,
            length=100,
            status="accepted",
            copied_text=self.text,
        )
        opinion = ReviewAssignment.objects.create(
            review=review, user=self.reader, position=1, opinion="yes", notes="Opinia"
        )
        url = reverse("core:link_text_review", args=[self.text.pk])
        token = self.token()
        data = {"action": "unlink", "review_id": review.pk, "_edit_version": token}
        self.client.post(url, data)
        review.refresh_from_db()
        self.assertEqual(review.copied_text_id, self.text.pk)
        response = self.client.post(url, {**data, "confirm_unlink": "on"})
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)
        self.assertTrue(review.publication_detached)
        self.assertEqual(review.status, "accepted")
        self.assertTrue(ReviewAssignment.objects.filter(pk=opinion.pk, notes="Opinia").exists())
        self.assertEqual(self.client.post(url, {**data, "confirm_unlink": "on"}).status_code, 409)
        link_source_review(
            user=self.admin, text_id=self.text.pk, review_id=review.pk, confirm_mismatch=True
        )
        review.refresh_from_db()
        self.assertFalse(review.publication_detached)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(url).status_code, 403)

    def make_review(self, title, status, opinion="reading", legacy=False, user=None):
        review = Review.objects.create(
            title=title, anthology=self.book, length=100, status=status, old_reviews=legacy
        )
        ReviewAssignment.objects.create(
            review=review, user=user or self.reader, position=1, opinion=opinion
        )
        return review

    def test_review_lists_split_by_decision_not_legacy_import_flag(self):
        current = self.make_review("Otwarty", "new")
        withdrawn = self.make_review("Wycofany", "withdrawn")
        accepted = self.make_review("Przyjęty", "accepted", "yes")
        rejected = self.make_review("Odrzucony", "rejected", "no")
        historic = self.make_review("Dawny przyjęty", "accepted", "yes", legacy=True)
        url = reverse("core:review_list")
        now = self.client.get(url)
        archive = self.client.get(url, {"old_reviews": "1"})
        # Withdrawn submissions are available only in the admin panel.
        self.assertEqual({x["pk"] for x in now.context["reviews"]}, {current.pk})
        self.assertNotIn(withdrawn.pk, {x["pk"] for x in archive.context["reviews"]})
        self.assertEqual(
            {x["pk"] for x in archive.context["reviews"]}, {accepted.pk, rejected.pk, historic.pk}
        )
        self.assertFalse(Review.objects.get(pk=accepted.pk).old_reviews)
        search = self.client.get(reverse("core:global_search"), {"q": "Przyjęty"})
        self.assertEqual({x["pk"] for x in search.context["reviews"]}, {accepted.pk, historic.pk})
        self.assertEqual(self.client.get(reverse("core:global_search"), {"q": "Wycofany"}).context["reviews"], [])
        self.assertContains(
            self.client.get(reverse("core:assigned_review_detail", args=[accepted.pk])),
            "Recenzja archiwalna",
        )

    def test_my_reviews_two_tabs_include_all_history_and_unrated_work(self):
        waiting = self.make_review("Oczekujący", "new")
        reading = self.make_review("Czytany", "in_review", "reading")
        rated = self.make_review("Oceniony przed decyzją", "new", "yes")
        accepted = self.make_review("Przyjęty", "accepted", "yes")
        historic = self.make_review("Importowany", "rejected", "no", legacy=True)
        other = self.make_review("Cudzy", "new", user=self.other)
        self.client.force_login(self.reader)
        url = reverse("core:my_reviews")
        active = self.client.get(url)
        all_page = self.client.get(url, {"view": "completed"})
        self.assertEqual(
            {x.review_id for x in active.context["assignments"]}, {waiting.pk, reading.pk}
        )
        self.assertNotIn(other.pk, {x.review_id for x in all_page.context["assignments"]})
        doc = html.fromstring(all_page.content)
        self.assertEqual(
            doc.xpath('//nav[@aria-label="Widok moich recenzji"]/a/text()'),
            ["W toku", "Oddane"],
        )
        for view in ["archived", "completed", "decided", "all"]:
            self.assertEqual(
                {x.review_id for x in self.client.get(url, {"view": view}).context["assignments"]},
                {rated.pk, accepted.pk, historic.pk},
            )

    def test_profile_archive_uses_decision_and_preserves_hidden_permissions(self):
        accepted = self.make_review("Przyjęty", "accepted", "yes")
        current = self.make_review("Wciąż oceniany", "in_review", "yes")
        hidden = self.make_review("Ukryty przyjęty", "accepted", "yes")
        hidden.is_hidden = True
        hidden.save()
        self.client.force_login(self.reader)
        page = self.client.get(reverse("core:person_detail", args=[self.reader.person_profile.pk]))
        self.assertEqual([a.review_id for a in page.context["archived_reviews"]], [accepted.pk])
        self.assertEqual([a.review_id for a in page.context["completed_reviews"]], [current.pk])

    def test_replacing_source_does_not_offer_previous_submission_for_duplicate_import(self):
        previous = Review.objects.create(title="Dawny tytuł", anthology=self.book, length=1,
                                         status="accepted", copied_text=self.text)
        new = Review.objects.create(title=self.text.title, anthology=self.book, length=1, status="accepted")
        link_source_review(user=self.admin, text_id=self.text.pk, review_id=new.pk, confirm_mismatch=True)
        previous.refresh_from_db()
        self.assertIsNone(previous.copied_text_id)
        self.assertTrue(previous.publication_detached)

    def test_abandoned_audio_metadata_remains_accessible_without_resuming_production(self):
        audio = Audiobook.objects.create(text=self.text, status="published")
        self.book.status = "abandoned"
        self.book.save()
        url = reverse("core:audiobook_detail", args=[self.text.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["can_edit"])
        self.assertFalse(page.context["can_manage_stages"])
        self.assertEqual(self.client.post(url, {"action": "people", "_edit_version": self.token(),
            "people-narrator_name": "Uzupełniony lektor"}).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.narrator_name, "Uzupełniony lektor")
        self.assertEqual(audio.status, "published")
