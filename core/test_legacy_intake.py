from email.message import EmailMessage
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from core.services.review_import_parser import parse_review_records
from core.services.mailbox_import import parse_message
from core.forms import ReviewBulkImportForm
from texts.models import Anthology

EXAMPLE='Renata Ościak;Dwadzieścia rys na amulecie;Grimdark [fantasy;37930;renata.osciak@gmail.com](mailto:fantasy%3B37930%3Brenata.osciak@gmail.com);727908378;Na pokład, psubraty;premierach, naborach\\\n&#x20;&#x20;'

class LegacyIntakeTests(TestCase):
    def test_exact_pasted_example(self):
        rows,errors=parse_review_records(EXAMPLE)
        self.assertFalse(errors)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["genre"], "Grimdark fantasy")
        self.assertEqual(row["length"], 37930)
        self.assertEqual(row["email"], "renata.osciak@gmail.com")
        self.assertTrue(row["newsletter_premieres"] and row["newsletter_recruitment"])
        self.assertEqual(row["phone_number"], "727908378")
        self.assertEqual(row["source_anthology"], "Na pokład, psubraty")

    def test_legacy_mail_retains_consents(self):
        msg = EmailMessage()
        msg["Subject"] = "Renata Ościak – Dwadzieścia rys na amulecie"
        msg.set_content(EXAMPLE)
        msg.add_attachment(
            b"data", maintype="application", subtype="octet-stream", filename="tekst.docx"
        )
        parsed = parse_message(1, msg.as_bytes())
        rows, errors = parse_review_records(parsed["record"])
        self.assertFalse(errors)
        self.assertTrue(rows[0]["newsletter_premieres"])
        self.assertTrue(rows[0]["newsletter_recruitment"])
        self.assertEqual(parsed["anthology"], "Na pokład, psubraty")

    def test_anthology_mismatch_rejected(self):
        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password")
        book = Anthology.objects.create(title="Inna antologia")
        form = ReviewBulkImportForm({"anthology": book.pk, "records": EXAMPLE}, user=user)
        self.assertFalse(form.is_valid())
        self.assertIn("nie odpowiada", str(form.errors))
        book.title = "Na pokład, psubraty"
        book.save()
        form = ReviewBulkImportForm({"anthology": book.pk, "records": EXAMPLE}, user=user)
        self.assertTrue(form.is_valid(), form.errors)

    def test_panels_closed_initially_bulk_first_and_errors_open(self):
        from html.parser import HTMLParser

        class Panels(HTMLParser):
            def __init__(self):
                super().__init__()
                self.panels = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "details" and "intake-panel" in attrs.get("class", ""):
                    self.panels.append(attrs)

        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password")
        self.client.force_login(user)
        url = reverse("core:review_create")
        response = self.client.get(url)
        parser = Panels()
        parser.feed(response.content.decode())
        self.assertEqual(len(parser.panels), 2)
        self.assertTrue(all("open" not in p for p in parser.panels))
        self.assertLess(
            response.content.index("Wiele zgłoszeń".encode()),
            response.content.index("Pojedyncze zgłoszenie".encode()),
        )
        response = self.client.post(url, {"title": "Błąd"})
        parser = Panels()
        parser.feed(response.content.decode())
        self.assertIn("open", parser.panels[1])
