from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile
from pathlib import Path
import shutil
import subprocess
from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, Client
from django.urls import reverse, resolve
from lxml import html
from core.activity import LABELS, describe_request
from core.activity_targets import target_for_match, present_activities
from core.models import Recruitment, MailboxDownload, RecruitmentMailSource, UserActivity
from core.services.recruitment_samples import attachment_archive
from core.services.mailbox_import import parse_message
from core.test_recruitment_samples import sample
from core import test_recruitment_samples as sample_tests
from core.views.recruitment_notified import notified_token
from people.models import Person, Role
from illustrations.models import Illustrator


class AttachmentExtractionTests(SimpleTestCase):
    def test_notification_browser_save_and_error_recovery(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for the browser regression test')
        result = subprocess.run([node, str(Path(__file__).parent / 'js_tests' / 'recruitment_notified.cjs')], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_formats_inline_nameless_and_forwarded_mail_are_downloaded(self):
        msg = EmailMessage()
        msg["From"] = "Jan Nowak <jan@example.test>"
        msg.set_content("Treść")
        for name, maintype, subtype in [
            ("stary.doc", "application", "msword"),
            ("nowy.docx", "application", "octet-stream"),
            ("podglad.pdf", "application", "pdf"),
            ("logo.png", "image", "png"),
            ("notatki.txt", "text", "plain"),
        ]:
            msg.add_attachment(
                name.encode(),
                maintype=maintype,
                subtype=subtype,
                filename=name,
                disposition="inline",
            )
        msg.add_attachment(b"unnamed", maintype="application", subtype="pdf")
        forwarded = EmailMessage()
        forwarded["Subject"] = "Wiadomość"
        forwarded.set_content("Dalej")
        forwarded.add_attachment(
            b"nested", maintype="application", subtype="pdf", filename="wewnetrzny.pdf"
        )
        msg.add_attachment(forwarded, filename="przekazana.eml")
        downloaded = set()
        with (
            attachment_archive(
                [{"uid": 1, "raw": msg.as_bytes()}], downloaded_uids=downloaded
            ) as stream,
            ZipFile(stream) as z,
        ):
            names = z.namelist()
            self.assertEqual(len(names), 7)
            self.assertEqual(z.read("Jan Nowak/stary.doc"), b"stary.doc")
            self.assertIn("Jan Nowak/logo.png", names)
            self.assertTrue(any(n.endswith(".pdf") and z.read(n) == b"unnamed" for n in names))
            self.assertIn(b"wewnetrzny.pdf", z.read("Jan Nowak/przekazana.eml"))
        self.assertEqual(downloaded, {1})

    def test_no_attachments_means_no_archive_or_placeholder_folder(self):
        no = sample(1)
        yes = sample(2, attachment=True)
        self.assertIsNone(attachment_archive([no]))
        with attachment_archive([no, yes]) as stream, ZipFile(stream) as z:
            self.assertEqual(z.namelist(), ["Kandydat/próbka.docx"])
        # A link in mail is not a MIME attachment and is not fetched automatically.
        msg = EmailMessage()
        msg.set_content("Plik 1: https://example.test/próbka.doc")
        self.assertIsNone(attachment_archive([{"uid": 3, "raw": msg.as_bytes()}]))

    def test_submission_mail_also_keeps_inline_documents(self):
        msg = EmailMessage()
        msg.set_content("Treść")
        msg.add_attachment(
            b"doc",
            maintype="application",
            subtype="msword",
            filename="stary.doc",
            disposition="inline",
        )
        self.assertEqual(
            parse_message(1, msg.as_bytes(), submission=False)["files"], [("stary.doc", b"doc")]
        )


class RecruitmentInlineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        sample_tests.RecruitmentSamplesTests.setUpTestData.__func__(cls)

    setUp = sample_tests.RecruitmentSamplesTests.setUp
    payload = sample_tests.RecruitmentSamplesTests.payload

    def test_inline_layout_and_notification(self):
        response = self.client.get(self.url)
        doc = html.fromstring(response.content)
        self.assertTrue(
            doc.xpath(
                '//div[contains(@class,"mailbox-options")][label/input[@name="show_downloaded"]]//button[@value="headers"]'
            )
        )
        self.assertTrue(
            doc.xpath('//select[@data-recruitment-notified]/option[@selected and @value="no"]')
        )
        with patch("core.views.recruitment_mailbox.fetch_messages", return_value=[sample()]):
            response = self.client.post(self.url, self.payload("set_notified", notified="yes"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["notified"])
        record = Recruitment.objects.get()
        self.assertTrue(record.notified)
        self.assertIsNone(record.accepted)
        self.assertFalse(MailboxDownload.objects.exists())
        page = self.client.get(reverse("core:recruitment_list"))
        self.assertContains(page, reverse("core:recruitment_notified", args=[record.pk]))
        self.assertContains(page, 'value="yes" selected')
        # Once imported, changing the flag does not require a live mailbox.
        with patch("core.views.recruitment_mailbox.fetch_messages") as fetch:
            response = self.client.post(
                reverse("core:recruitment_notified", args=[record.pk]),
                {"notified": "no", "version": response.json()["version"]},
            )
            self.assertEqual(response.status_code, 200)
            fetch.assert_not_called()
        record.refresh_from_db()
        self.assertFalse(record.notified)
        self.assertIsNone(record.notified_at)

    def test_mixed_download(self):
        with patch(
            "core.views.recruitment_mailbox.fetch_messages",
            return_value=[sample(12), sample(13, attachment=True)],
        ):
            response = self.client.post(self.url, self.payload("download", uids=(12, 13)))
        with ZipFile(BytesIO(b"".join(response.streaming_content))) as z:
            self.assertEqual(z.namelist(), ["Kandydat/próbka.docx"])
        self.assertEqual(Recruitment.objects.count(), 2)
        self.assertEqual(set(MailboxDownload.objects.values_list("uid", flat=True)), {13})
        self.assertIsNone(RecruitmentMailSource.objects.get(uid=12).downloaded_at)


class NotificationPermissionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("coord")
        p = Person.objects.create(user=self.user, first_name="Jan", last_name="Koordynator")
        p.roles.add(Role.objects.get_or_create(name="Koordynator rekrutacji")[0])
        self.record = Recruitment.objects.create(
            mail_roles=["editors", "proofreaders"],
            notes="Nie zmieniaj",
            unofficial_notes="Prywatne",
        )
        self.url = reverse("core:recruitment_notified", args=[self.record.pk])
        self.client.force_login(self.user)

    def test_only_flag_changes_and_stale_or_forged_tokens_rejected(self):
        token = notified_token(self.user, self.record)
        before = list(
            self.record.role_decisions.values(
                "role", "status", "decision_reason", "unofficial_notes"
            )
        )
        response = self.client.post(self.url, {"notified": "yes", "version": token})
        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        date = self.record.notified_at
        self.assertEqual(self.record.notes, "Nie zmieniaj")
        self.assertEqual(
            list(
                self.record.role_decisions.values(
                    "role", "status", "decision_reason", "unofficial_notes"
                )
            ),
            before,
        )
        self.assertEqual(
            self.client.post(self.url, {"notified": "no", "version": token}).status_code, 409
        )
        self.assertEqual(
            self.client.post(self.url, {"notified": "yes", "version": "forged"}).status_code, 409
        )
        self.assertEqual(
            self.client.post(
                self.url, {"notified": "invalid", "version": response.json()["version"]}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                self.url, {"notified": "yes", "version": response.json()["version"]}
            ).status_code,
            200,
        )
        self.record.refresh_from_db()
        self.assertEqual(self.record.notified_at, date)

    def test_permissions_csrf_and_post_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url, {"notified": "yes"}).status_code, 403)
        self.user.person_profile.roles.clear()
        self.assertEqual(self.client.post(self.url, {"notified": "yes"}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"notified": "yes"}).status_code, 302)


class ActivityLabelsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            "activityadmin", "admin@example.test", "test"
        )
        self.person = Illustrator.objects.create(first_name="Anna", last_name="Rysuje")

    def test_all_named_routes_have_polish_labels_and_admin_uses_model_name(self):
        from core.urls import urlpatterns
        from illustrations.urls import urlpatterns as illustration_patterns
        from core.auth_urls import urlpatterns as auth_patterns

        for route in [*urlpatterns, *illustration_patterns, *auth_patterns]:
            if route.name:
                self.assertIn(route.name, LABELS)
        self.assertEqual(
            describe_request(resolve(reverse("illustrations:illustrator_list")), "GET")[0],
            "Ilustratorzy",
        )
        self.assertIn(
            "ilustrator",
            describe_request(
                resolve(reverse("admin:illustrations_illustrator_change", args=[self.person.pk])),
                "POST",
            )[0].casefold(),
        )

    def test_old_rows_are_readable_searchable_and_object_names_escaped(self):
        UserActivity.objects.create(
            user=self.admin,
            actor="Admin",
            method="GET",
            action="illustrator list",
            target="",
            path=reverse("illustrations:illustrator_list"),
            status_code=200,
        )
        entry = UserActivity.objects.create(
            user=self.admin,
            actor="Admin",
            method="POST",
            action="Próba / formularz: illustrator edit",
            target=f"#{self.person.pk}",
            path=reverse("illustrations:illustrator_edit", args=[self.person.pk]),
            status_code=200,
        )
        self.client.force_login(self.admin)
        page = self.client.get(reverse("core:user_activity"))
        self.assertContains(page, "Anna Rysuje")
        self.assertContains(page, "Ilustratorzy")
        self.assertNotContains(page, "illustrator list")
        self.assertContains(
            self.client.get(reverse("core:user_activity"), {"q": "Ilustratorzy"}), "Ilustratorzy"
        )
        entry.target = "Zachowana nazwa (#1)"
        self.person.delete()
        present_activities([entry])
        self.assertEqual(entry.display_target, entry.target)

    def test_new_activity_snapshots_object_name_not_post_body(self):
        self.client.force_login(self.admin)
        with patch("core.activity_spool.enqueue_activity") as queue:
            self.client.get(reverse("illustrations:illustrator_edit", args=[self.person.pk]))
        self.assertIn("Anna Rysuje", queue.call_args.kwargs["target"])
        match = resolve(reverse("illustrations:illustrator_edit", args=[self.person.pk]))
        self.assertIn("Anna Rysuje", target_for_match(match))
