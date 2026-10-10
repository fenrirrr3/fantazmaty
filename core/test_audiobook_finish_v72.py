from datetime import timedelta

from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage
from texts.models import Anthology, Text
from workflow.tests import create_member


class AudioFinishV72Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = create_member("v72reader", "Korektor audiobooków")
        cls.other = create_member("v72other", "Korektor audiobooków")
        cls.manager = create_member("v72manager", "Koordynator korekty")
        cls.book = Anthology.objects.create(title="Książka testowa", status="ready")
        cls.text = Text.objects.create(title="Korekta do oddania", anthology=cls.book, length=1)
        cls.stage = AudiobookStage.objects.create(
            text=cls.text,
            stage_type="proofreading",
            performer=cls.reader,
            started_at=timezone.localdate() - timedelta(days=3),
        )
        cls.audio = Audiobook.objects.create(
            text=cls.text, status="proofreading", proofreader=cls.reader, active_stage=cls.stage
        )
        cls.queue = reverse("core:audio_proofreading")
        cls.detail = reverse("core:audiobook_detail", args=[cls.text.pk])

    def token(self, user):
        return signing.dumps(
            [user.pk, f"texts.text:{self.text.pk}", version_of(self.text)], salt="cms-edit-version"
        )

    def payload(self, user):
        return {
            "action": "finish_stage",
            "stage_id": self.stage.pk,
            "return_to": "audio_proofreading",
            "_edit_version": self.token(user),
        }

    def test_finish_from_rendered_table_form(self):
        self.client.force_login(self.reader)
        page = self.client.get(self.queue)
        form = html.fromstring(page.content).xpath(
            '//form[input[@name="action" and @value="finish_stage"]]'
        )[0]
        self.assertEqual(form.xpath(".//button/text()"), ["Zakończ"])
        payload = {i.get("name"): i.get("value") for i in form.xpath(".//input[@name]")}
        response = self.client.post(form.get("action"), payload)
        self.assertRedirects(response, self.queue)
        self.stage.refresh_from_db()
        self.audio.refresh_from_db()
        self.assertEqual(self.stage.ended_at, timezone.localdate())
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.performer, self.reader)
        self.assertIsNone(self.audio.active_stage_id)
        self.assertEqual(self.client.post(self.detail, payload).status_code, 409)
        self.assertNotContains(
            self.client.get(self.queue, {"hide_completed": "0"}),
            'name="action" value="finish_stage"',
        )

    def test_other_reader_cannot_finish_and_disabled_work_is_protected(self):
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(self.queue), "Korekta do oddania")
        self.assertEqual(self.client.post(self.detail, self.payload(self.other)).status_code, 403)
        self.text.audiobook_blacklisted = True
        self.text.save()
        self.client.force_login(self.reader)
        self.assertNotContains(self.client.get(self.queue), 'name="action" value="finish_stage"')
        self.assertEqual(self.client.post(self.detail, self.payload(self.reader)).status_code, 403)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_reassignment_rejects_previous_reader(self):
        self.client.force_login(self.reader)
        payload = self.payload(self.reader)
        self.audio.proofreader = self.other
        self.audio.save()
        self.assertEqual(self.client.post(self.detail, payload).status_code, 403)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_detail_label_and_unknown_completion_date(self):
        historical = AudiobookStage.objects.create(
            text=self.text, stage_type="recording", is_completed=True
        )
        self.client.force_login(self.reader)
        page = self.client.get(self.detail)
        doc = html.fromstring(page.content)
        self.assertEqual(
            doc.xpath('//button[contains(@class,"audio-finish-button")]/text()'), ["Zakończ"]
        )
        cell = doc.xpath('//table/tbody/tr[td[contains(.,"Trwa nagrywanie")]]/td[3]')[0]
        self.assertEqual("".join(cell.itertext()).strip(), "Zakończony: data nieznana")
        historical.ended_at = timezone.localdate()
        historical.save()
        page = self.client.get(self.detail)
        cell = html.fromstring(page.content).xpath(
            '//table/tbody/tr[td[contains(.,"Trwa nagrywanie")]]/td[3]'
        )[0]
        self.assertEqual(
            "".join(cell.itertext()).strip(), timezone.localdate().strftime("%d.%m.%Y")
        )

    def test_coordinator_can_finish_from_table(self):
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(self.queue), 'name="action" value="finish_stage"')
        response = self.client.post(self.detail, self.payload(self.manager))
        self.assertRedirects(response, self.queue)
