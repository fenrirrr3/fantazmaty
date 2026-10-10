from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.selectors.people import profile_assignments
from illustrations.models import Illustrator
from texts.cover_forms import CoverAssignmentForm
from texts.models import Anthology, ExtractVolume, ExtractVolumeCredit
from workflow.tests import create_member


class CoverAndExtractTests(TestCase):
    def test_legacy_cover_name_is_escaped_in_form_help(self):
        self.book.cover_author = '<script>alert(1)</script>'
        markup = str(CoverAssignmentForm(instance=self.book).as_p())
        self.assertNotIn('<script>alert(1)</script>', markup)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', markup)

    def setUp(self):
        self.manager = create_member("cover-manager", "Koordynator redakcji")
        self.member = create_member("cover-member", "Redaktor")
        self.book = Anthology.objects.create(title="Antologia z okładką")
        self.url = reverse("core:anthology_detail", args=[self.book.pk])

    def token(self, user):
        return signing.dumps(
            [user.pk, f"texts.anthology:{self.book.pk}", version_of(self.book)],
            salt="cms-edit-version",
        )

    def post_cover(self, user=None, **values):
        user = user or self.manager
        self.client.force_login(user)
        return self.client.post(
            self.url,
            {
                "action": "cover",
                "_edit_version": self.token(user),
                "cover-cover_status": "in_progress",
                **{f"cover-{k}": v for k, v in values.items()},
            },
        )

    def test_inactive_artist_is_searchable_and_assignment_reuses_existing_contact(self):
        artist = Illustrator.objects.create(
            first_name="Anna", last_name="Rysuje", is_active=False, email="anna@example.test"
        )
        self.client.force_login(self.manager)
        page = self.client.get(self.url)
        self.assertContains(page, "Szukaj ilustratora, także nieaktywnego")
        self.assertContains(page, "Anna Rysuje – nieaktywny")
        self.assertEqual(self.post_cover(cover_illustrator=artist.pk).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(
            (self.book.cover_illustrator, self.book.cover_author, self.book.cover_status),
            (artist, "Anna Rysuje", "in_progress"),
        )
        artist.refresh_from_db()
        self.assertFalse(artist.is_active)
        self.assertEqual(artist.email, "anna@example.test")
        tasks = self.client.get(reverse("core:task_list"))
        self.assertContains(tasks, "Anna Rysuje")
        self.assertContains(tasks, "Zlecone")

    def test_new_artist_has_no_email_is_inactive_and_repeat_is_rejected(self):
        stale = self.token(self.manager)
        response = self.post_cover(
            new_cover_first_name="Nowa", new_cover_last_name="Osoba", cover_status="ready"
        )
        self.assertEqual(response.status_code, 302)
        artist = Illustrator.objects.get(first_name="Nowa")
        self.assertIsNone(artist.email)
        self.assertFalse(artist.is_active)
        self.assertTrue(artist.covers)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_illustrator, artist)
        self.assertEqual(self.book.cover_status, "ready")
        self.assertEqual(
            self.client.post(
                self.url,
                {
                    "action": "cover",
                    "_edit_version": stale,
                    "cover-cover_status": "ready",
                    "cover-new_cover_first_name": "Nowa",
                },
            ).status_code,
            409,
        )
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_invalid_or_forbidden_save_never_creates_contact(self):
        self.assertEqual(
            self.post_cover(
                new_cover_first_name="Nie zapisuj", cover_status="not_started"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post_cover(user=self.member, new_cover_first_name="Bez dostępu").status_code, 403
        )
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.post(
                self.url, {"action": "cover", "cover-new_cover_first_name": "Bez wersji"}
            ).status_code,
            409,
        )
        self.assertFalse(Illustrator.objects.exists())

    def test_existing_name_or_alias_and_mixed_selection_are_rejected(self):
        artist = Illustrator.objects.create(
            first_name="Jan", last_name="Kowalski", pseudonym="Graphos"
        )
        for values in (
            {"new_cover_first_name": "jan", "new_cover_last_name": "Kowalski"},
            {"new_cover_first_name": "Graphos"},
            {"cover_illustrator": artist.pk, "new_cover_first_name": "Druga"},
        ):
            self.assertEqual(self.post_cover(**values).status_code, 400)
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_legacy_names_survive_and_can_be_explicitly_cleared(self):
        self.book.cover_author = "Historyczny podpis"
        self.book.cover_status = "ready"
        self.book.save()
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertContains(response, "Dotychczasowy zapis: Historyczny podpis")
        self.assertEqual(
            self.post_cover(cover_status="ready", cover_notes="Dodatkowe uwagi").status_code, 302
        )
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, "Historyczny podpis")
        self.assertEqual(
            self.post_cover(cover_status="not_started", clear_legacy_cover="on").status_code, 302
        )
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, "")

    def test_commit_false_creates_no_contact_and_unassign_clears_name(self):
        form = CoverAssignmentForm(
            {"cover_status": "in_progress", "new_cover_first_name": "Odłożona"}, instance=self.book
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)
        self.assertFalse(Illustrator.objects.exists())
        self.assertEqual(self.post_cover(new_cover_first_name="Zapisana").status_code, 302)
        self.assertEqual(
            self.post_cover(cover_status="not_started", cover_illustrator="").status_code, 302
        )
        self.book.refresh_from_db()
        self.assertIsNone(self.book.cover_illustrator_id)
        self.assertEqual(self.book.cover_author, "")
        self.assertTrue(Illustrator.objects.filter(first_name="Zapisana").exists())

    def test_extract_profile_only_text_roles_and_hidden_sections_for_volumes_one_two(self):
        person = self.member.person_profile
        permitted = ("Redakcja", "Korekta", "Weryfikacja", "Korekta poskładowa")
        self.client.force_login(self.manager)
        for number in (1, 2, 3):
            book = Anthology.objects.create(title=f"Ekstrakty {number}")
            ExtractVolume.objects.create(anthology=book, number=number)
            for role in (
                *permitted,
                "Koordynacja działu redakcji",
                "Redaktor prowadzący",
                "Skład",
                "Audiodeskrypcja",
            ):
                ExtractVolumeCredit.objects.create(anthology=book, person=person, role=role)
            page = self.client.get(reverse("core:anthology_detail", args=[book.pk]))
            if number in (1, 2):
                self.assertNotContains(page, "Osoby i wykonane prace")
            else:
                self.assertContains(page, "Osoby i wykonane prace")
        rows, summary = profile_assignments(person, include_authors=True)
        self.assertEqual({r["role"] for r in rows}, set(permitted))
        self.assertEqual(len(rows), 12)
        self.assertEqual(summary["completed"], 3)
        self.assertEqual(ExtractVolumeCredit.objects.count(), 24)
        self.assertContains(self.client.get(self.url), "Osoby i wykonane prace")

    def test_admin_saves_cover_after_validation_of_all_inlines(self):
        admin = get_user_model().objects.create_superuser("cover-admin", "", "test")
        self.client.force_login(admin)
        url = reverse("admin:texts_anthology_change", args=[self.book.pk])

        def form_data():
            doc = html.fromstring(self.client.get(url).content)
            form = doc.xpath('//form[@id="anthology_form"]')[0]
            data = {}
            for field in form.xpath(".//input[@name] | .//select[@name] | .//textarea[@name]"):
                if field.get("type") in ("checkbox", "radio") and field.get("checked") is None:
                    continue
                if field.tag == "select":
                    options = field.xpath("./option[@selected]") or field.xpath("./option")[:1]
                    value = options[0].get("value", "") if options else ""
                else:
                    value = field.get("value", "") if field.tag == "input" else field.text or ""
                data[field.get("name")] = value
            data.update(
                cover_status="in_progress",
                new_cover_first_name="Administrator",
                new_cover_last_name="Dodał",
                _save="Zapisz",
            )
            return data

        data = form_data()
        invalid = {**data, "production_tasks-0-status": "ready"}
        self.assertEqual(self.client.post(url, invalid).status_code, 200)
        self.assertFalse(Illustrator.objects.exists())
        response = self.client.post(url, form_data())
        self.assertEqual(response.status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, "Administrator Dodał")
        self.assertFalse(self.book.cover_illustrator.is_active)
