"""Okładka antologii: wybór ilustratora, nowy kontakt z formularza okładki, dawne podpisy, data zlecenia i zapis w adminie."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from illustrations.models import Illustrator
from texts.cover_forms import CoverAssignmentForm
from texts.models import Anthology

from .support import AnthologyPageData, admin_form_data


class CoverAssignmentTests(AnthologyPageData, TestCase):
    def test_legacy_cover_name_is_escaped_in_form_help(self):
        """Błąd: dawny podpis okładki trafiał do podpowiedzi formularza bez escapowania (XSS)."""
        self.book.cover_author = '<script>alert(1)</script>'
        markup = str(CoverAssignmentForm(instance=self.book).as_p())
        self.assertNotIn('<script>alert(1)</script>', markup)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', markup)

    def test_inactive_artist_is_searchable_and_assignment_reuses_existing_contact(self):
        artist = Illustrator.objects.create(first_name="Anna", last_name="Rysuje", is_active=False,
                                            email="anna@example.test")
        page = self.client.get(self.url)
        self.assertContains(page, "Szukaj ilustratora, także nieaktywnego")
        self.assertContains(page, "Anna Rysuje – nieaktywny")
        self.assertEqual(self.post_cover(cover_illustrator=artist.pk).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual((self.book.cover_illustrator, self.book.cover_author, self.book.cover_status),
                         (artist, "Anna Rysuje", "in_progress"))
        artist.refresh_from_db()
        self.assertFalse(artist.is_active)
        self.assertEqual(artist.email, "anna@example.test")
        tasks = self.client.get(reverse("core:task_list"))
        self.assertContains(tasks, "Anna Rysuje")
        self.assertContains(tasks, "Zlecone")

    def test_new_artist_has_no_email_is_inactive_and_repeat_is_rejected(self):
        stale = self.token()
        response = self.post_cover(new_cover_first_name="Nowa", new_cover_last_name="Osoba", cover_status="ready")
        self.assertEqual(response.status_code, 302)
        artist = Illustrator.objects.get(first_name="Nowa")
        self.assertIsNone(artist.email)
        self.assertFalse(artist.is_active)
        self.assertTrue(artist.covers)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_illustrator, artist)
        self.assertEqual(self.book.cover_status, "ready")
        response = self.client.post(self.url, {"action": "cover", "_edit_version": stale,
                                               "cover-cover_status": "ready", "cover-new_cover_first_name": "Nowa"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Illustrator.objects.count(), 1)

    def test_invalid_or_forbidden_save_never_creates_contact(self):
        response = self.post_cover(new_cover_first_name="Nie zapisuj", cover_status="not_started")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post_cover(user=self.member, new_cover_first_name="Bez dostępu").status_code, 403)
        self.client.force_login(self.manager)
        response = self.client.post(self.url, {"action": "cover", "cover-new_cover_first_name": "Bez wersji"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Illustrator.objects.exists())

    def test_existing_name_or_alias_and_mixed_selection_are_rejected(self):
        artist = Illustrator.objects.create(first_name="Jan", last_name="Kowalski", pseudonym="Graphos")
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
        self.assertContains(self.client.get(self.url), "Dotychczasowy zapis: Historyczny podpis")
        self.assertEqual(self.post_cover(cover_status="ready", cover_notes="Dodatkowe uwagi").status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, "Historyczny podpis")
        self.assertEqual(self.post_cover(cover_status="not_started", clear_legacy_cover="on").status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, "")

    def test_commit_false_creates_no_contact_and_unassign_clears_name(self):
        form = CoverAssignmentForm({"cover_status": "in_progress", "new_cover_first_name": "Odłożona"},
                                   instance=self.book)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)
        self.assertFalse(Illustrator.objects.exists())
        self.assertEqual(self.post_cover(new_cover_first_name="Zapisana").status_code, 302)
        self.assertEqual(self.post_cover(cover_status="not_started", cover_illustrator="").status_code, 302)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.cover_illustrator_id)
        self.assertEqual(self.book.cover_author, "")
        self.assertTrue(Illustrator.objects.filter(first_name="Zapisana").exists())

    def test_cover_commission_date_survives_corrections_and_resets_for_new_artist(self):
        artist = Illustrator.objects.create(first_name='Ola', last_name='Okładka', covers=True)
        self.book.cover_illustrator, self.book.cover_status = artist, 'in_progress'
        self.book.save()
        old = timezone.localdate() - timedelta(days=10)
        Anthology.objects.filter(pk=self.book.pk).update(cover_commissioned_at=old)
        book = Anthology.objects.get(pk=self.book.pk)
        book.cover_notes = 'poprawka'
        book.cover_author = 'Ola Okładka (poprawione)'
        book.save()
        self.assertEqual(Anthology.objects.get(pk=book.pk).cover_commissioned_at, old)
        book.cover_illustrator = Illustrator.objects.create(first_name='Inna', last_name='Osoba', covers=True)
        book.save()
        self.assertEqual(Anthology.objects.get(pk=book.pk).cover_commissioned_at, timezone.localdate())

    def test_admin_saves_cover_after_validation_of_all_inlines(self):
        self.client.force_login(self.admin)
        url = reverse("admin:texts_anthology_change", args=[self.book.pk])

        def form_data():
            return admin_form_data(self.client, url, "anthology_form", cover_status="in_progress",
                                   new_cover_first_name="Administrator", new_cover_last_name="Dodał", _save="Zapisz")

        invalid = {**form_data(), "production_tasks-0-status": "ready"}
        self.assertEqual(self.client.post(url, invalid).status_code, 200)
        self.assertFalse(Illustrator.objects.exists())
        response = self.client.post(url, form_data())
        self.assertEqual(response.status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, "Administrator Dodał")
        self.assertFalse(self.book.cover_illustrator.is_active)
