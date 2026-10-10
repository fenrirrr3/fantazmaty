import uuid
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.models import Audiobook, AudiobookStage, PostLayoutAssignment
from core.post_layout import AssignmentForm, create_assignment
from core.source_reviews import linkable_reviews, link_source_review
from illustrations.models import Illustrator
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowStage, WorkflowRoleAssignment, WorkflowHandoff
from workflow.tests import create_member


class ChangesV79Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("v79-admin", "", "test")
        cls.manager = create_member("v79-manager", "Koordynator korekty")
        cls.old = create_member("v79-old", "Redaktor")
        cls.new = create_member("private-login@example.test", "Redaktor")
        profile = cls.new.person_profile
        profile.first_name, profile.last_name = "Anna", "Nowa"
        profile.save()
        cls.reader = create_member("v79-reader", "Korektor poskładowy")
        cls.book = Anthology.objects.create(title="Fantazmaty 6")
        cls.original = Anthology.objects.create(title="Fantazmaty 4", status="ready")
        cls.text = Text.objects.create(
            title="Opowiadanie przeniesione", anthology=cls.book, length=100
        )
        cls.author = Author.objects.create(
            first_name="Jan", last_name="Autor", email="author@example.test"
        )
        cls.text.authors.add(cls.author)
        cls.assignment = WorkflowRoleAssignment.objects.create(
            text=cls.text, role="editor", assigned_to=cls.old
        )
        cls.stage = WorkflowStage.objects.create(
            text=cls.text,
            stage_type="editing",
            assignment=cls.assignment,
            started_at=timezone.localdate(),
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_handoff_dropdown_names_and_optional_reason_preserve_history(self):
        url = reverse("core:handoff_workflow_stage", args=[self.stage.pk])
        response = self.client.get(url)
        doc = html.fromstring(response.content)
        self.assertEqual(
            doc.xpath(f'//select[@name="assigned_to"]/option[@value="{self.new.pk}"]/text()'),
            ["Anna Nowa"],
        )
        self.assertFalse(doc.xpath('//textarea[@name="reason"]/@required'))
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(
            url,
            {
                "assigned_to": self.new.pk,
                "expected_assignment_id": self.assignment.pk,
                "_edit_version": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        handoff = WorkflowHandoff.objects.get(text=self.text)
        self.assertEqual(handoff.reason, "")
        handoff.full_clean()
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.is_current)
        self.assertEqual(handoff.original_started_at, timezone.localdate())
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.assignment.assigned_to_id, self.new.pk)
        self.assertIsNone(self.stage.started_at)
        self.client.force_login(self.old)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_cross_anthology_source_search_and_save_dont_move_original_submission(self):
        review = Review.objects.create(
            title=self.text.title,
            anthology=self.original,
            length=100,
            author=self.author,
            status="accepted",
            old_reviews=True,
        )
        url = reverse("core:link_text_review", args=[self.text.pk])
        response = self.client.get(url, {"q": "przeniesione"})
        self.assertContains(response, f'id="source-{review.pk}"')
        self.assertContains(response, "Antologia zgłoszenia")
        self.assertContains(response, self.original.title)
        token = html.fromstring(response.content).xpath('//input[@name="_edit_version"]/@value')[0]
        saved = self.client.post(url, {"review_id": review.pk, "_edit_version": token})
        self.assertEqual(saved.status_code, 302)
        review.refresh_from_db()
        self.text.refresh_from_db()
        self.assertEqual(review.copied_text_id, self.text.pk)
        self.assertEqual(review.anthology_id, self.original.pk)
        self.assertEqual(self.text.anthology_id, self.book.pk)
        self.assertEqual(review.status, "accepted")
        self.assertTrue(review.old_reviews)

    def test_linking_still_checks_ownership_and_actual_review_decision(self):
        review = Review.objects.create(
            title=self.text.title,
            anthology=self.original,
            length=100,
            author=self.author,
            status="rejected",
            old_reviews=True,
        )
        self.assertIn(review, linkable_reviews(self.text))
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=review.pk)
        link_source_review(
            user=self.admin, text_id=self.text.pk, review_id=review.pk, confirm_mismatch=True
        )
        other = Text.objects.create(title="Inny tekst", anthology=self.book, length=10)
        self.assertNotIn(review, linkable_reviews(other))
        with self.assertRaises(ValidationError):
            link_source_review(
                user=self.admin, text_id=other.pk, review_id=review.pk, confirm_mismatch=True
            )

    def test_post_layout_creation_excludes_ready_abandoned_and_stale_form(self):
        self.book = Anthology.objects.create(title="Tom korekty poskładowej")
        abandoned = Anthology.objects.create(title="Porzucona", status="abandoned")
        form = AssignmentForm(user=self.manager)
        self.assertIn(self.book, form.fields["anthology"].queryset)
        self.assertNotIn(self.original, form.fields["anthology"].queryset)
        self.assertNotIn(abandoned, form.fields["anthology"].queryset)
        data = {
            "anthology": self.book.pk,
            "proofreader": self.reader.pk,
            "page_from": 1,
            "page_to": 2,
            "token": form.initial["token"],
        }
        bound = AssignmentForm(data, user=self.manager)
        self.assertTrue(bound.is_valid(), bound.errors)
        self.book.status = "ready"
        self.book.save()
        with self.assertRaises(ValidationError):
            create_assignment(user=self.manager, **bound.cleaned_data)
        for book in (self.original, abandoned):
            invalid = AssignmentForm({**data, "anthology": book.pk}, user=self.manager)
            self.assertFalse(invalid.is_valid())
        self.assertFalse(PostLayoutAssignment.objects.exists())

    def test_existing_post_layout_of_ready_anthology_stays_readable_and_editable(self):
        self.book = Anthology.objects.create(title="Tom korekty poskładowej")
        item = create_assignment(
            user=self.manager,
            anthology=self.book,
            proofreader=self.reader,
            page_from=1,
            page_to=10,
            token=uuid.uuid4(),
        )
        self.book.status = "ready"
        self.book.save()
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("core:post_layout")), self.book.title)
        self.assertEqual(
            self.client.get(reverse("core:post_layout_edit", args=[item.pk])).status_code, 200
        )

    def test_search_category_order_and_shared_empty_and_result_markup(self):
        url = reverse("core:global_search")
        doc = html.fromstring(self.client.get(url, {"q": "ZZZnothingmatch"}).content)
        sections = doc.xpath('//div[@class="global-search-results"]/section')
        expected = [
            "Bieżące recenzje",
            "Teksty",
            "Autorzy",
            "Zespół",
            "Antologie",
            "Powieści",
            "Lektorzy i montaż",
            "Ilustratorzy",
            "Rekrutacja",
        ]
        self.assertEqual([section.xpath("string(.//h2)") for section in sections], expected)
        self.assertTrue(
            all(section.xpath('./div[@class="empty-results-panel"]') for section in sections)
        )
        from core.models import AudioContributor, Recruitment

        AudioContributor.objects.create(name="Wspólny Lektor")
        Illustrator.objects.create(first_name="Wspólny", last_name="Ilustrator")
        Recruitment.objects.create(
            first_name="Wspólny", last_name="Kandydat", email="candidate@example.test"
        )
        Anthology.objects.create(title="Wspólny tytuł powieści", is_novel=True)
        doc = html.fromstring(self.client.get(url, {"q": "Wspólny"}).content)
        for label in expected[-4:]:
            section = doc.xpath('//section[header/h2[text()="' + label + '"]]')[0]
            self.assertTrue(
                section.xpath('./ul[@class="search-result-list"]/li[@class="search-result-item"]')
            )

    def test_history_panels_are_siblings_and_initially_collapsed(self):
        response = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
        doc = html.fromstring(response.content)
        panels = doc.xpath('//div[@data-detail-pair="assignment-history"]/details')
        self.assertEqual(
            [panel.get("id") for panel in panels], ["handoff-history", "assignment-history"]
        )
        self.assertTrue(all(panel.get("open") is None for panel in panels))
        self.assertEqual(len(doc.xpath('//*[@id="assignment-history"]')), 1)

    def test_audio_history_has_dates_but_no_duplicate_performer(self):
        person = create_member("Korektorka", "Korektor audiobooków")
        audio = Audiobook.objects.create(text=self.text, status="proofreading", proofreader=person)
        stage = AudiobookStage.objects.create(
            text=self.text,
            stage_type="proofreading",
            performer=person,
            started_at=timezone.localdate(),
        )
        audio.active_stage = stage
        audio.save()
        response = self.client.get(reverse("core:audio_proofreading"))
        doc = html.fromstring(response.content)
        history = doc.xpath('//ul[@class="audio-correction-history"]')[0]
        self.assertNotIn("Korektorka", history.text_content())
        self.assertFalse(history.xpath(".//a"))
        self.assertIn("Od:", history.text_content())
        self.assertContains(response, str(person.person_profile))

    def test_published_anthology_notice_is_under_the_heading(self):
        response = self.client.get(reverse("core:anthology_corrections"))
        doc = html.fromstring(response.content)
        self.assertIn(
            "Tu zgłaszamy uwagi tylko do już wydanych antologii",
            doc.xpath('string(//header[h1="Uwagi do antologii"]/p)'),
        )
