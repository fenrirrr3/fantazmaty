from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.edit_versions import version_of
from core.models import AudioDescriptionNote
from texts.models import Anthology
from workflow.tests import create_member


class AudioDescriptionContentTests(TestCase):
    def setUp(self):
        self.writer = create_member("adwriter", "Redaktor")
        self.controller = create_member("adcontroller", "Korektor")
        self.manager = create_member("admanager", "Koordynator redakcji")
        self.other = create_member("adother", "Redaktor")
        self.admin = get_user_model().objects.create_superuser("adadmin", "", "test")
        self.book = Anthology.objects.create(title="Audiodeskrypcja testowa")
        self.description = self.book.audio_description
        self.task = self.book.production_tasks.get(task_type="audio_description")
        self.task.assigned_to = self.writer.person_profile
        self.task.status = "commissioned"
        self.task.save()
        self.description.controllers.add(self.controller.person_profile)
        self.url = reverse("core:audio_description_detail", args=[self.book.pk])

    def token(self, user):
        return signing.dumps(
            [user.pk, f"texts.anthology:{self.book.pk}", version_of(self.book)],
            salt="cms-edit-version",
        )

    def post(self, user, **data):
        self.client.force_login(user)
        return self.client.post(self.url, {"_edit_version": self.token(user), **data})

    def test_all_four_authorized_groups_edit_content_but_other_member_cannot(self):
        for user in (self.writer, self.controller, self.manager, self.admin):
            response = self.post(user, action="content", content=f"Treść {user.pk}\nDrugi akapit.")
            self.assertEqual(response.status_code, 302)
            self.description.refresh_from_db()
            self.assertEqual(self.description.content, f"Treść {user.pk}\nDrugi akapit.")
        self.assertEqual(
            self.post(self.other, action="content", content="Nadpisane").status_code, 403
        )
        self.client.force_login(self.other)
        response = self.client.get(self.url)
        self.assertContains(response, "Drugi akapit.")
        self.assertNotContains(response, 'name="content"')

    def test_stage_finishes_task_and_reopening_in_either_place_keeps_consistency(self):
        for stage in ("consultation", "proofreading", "completed"):
            self.assertEqual(
                self.post(self.controller, action="stage", stage=stage).status_code, 302
            )
            self.task.refresh_from_db()
            self.assertEqual(self.task.status, "ready" if stage == "completed" else "commissioned")
        self.assertEqual(
            self.post(self.writer, action="stage", stage="proofreading").status_code, 302
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "commissioned")
        self.task.status = "ready"
        self.task.save()  # Same path as admin and anthology task forms.
        self.description.refresh_from_db()
        self.assertEqual(self.description.stage, "completed")
        self.task.status = "commissioned"
        self.task.save()
        self.description.refresh_from_db()
        self.assertEqual(self.description.stage, "writing")

    def test_finishing_unassigned_and_invalid_stage_do_not_save(self):
        self.task.assigned_to = None
        self.task.status = "not_commissioned"
        self.task.save()
        for stage in ("completed", "invalid"):
            self.assertEqual(self.post(self.manager, action="stage", stage=stage).status_code, 400)
        self.task.refresh_from_db()
        self.description.refresh_from_db()
        self.assertEqual(self.task.status, "not_commissioned")
        self.assertEqual(self.description.stage, "writing")

    def test_notes_stamp_real_author_and_time_escape_html_and_preserve_previous_entries(self):
        before = timezone.now()
        response = self.post(
            self.controller,
            action="note",
            note="<script>alert(1)</script> Uwaga",
            author=self.admin.pk,
            author_name="Podszycie",
            created_at="2000-01-01",
        )
        self.assertEqual(response.status_code, 302)
        note = self.description.notes.get()
        self.assertEqual(note.author, self.controller)
        self.assertEqual(note.author_name, str(self.controller.person_profile))
        self.assertGreaterEqual(note.created_at, before)
        self.assertEqual(self.post(self.writer, action="note", note="Druga uwaga").status_code, 302)
        self.assertEqual(self.description.notes.count(), 2)
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertEqual(self.post(self.writer, action="note", note="   ").status_code, 400)
        self.assertEqual(self.description.notes.count(), 2)

    def test_notes_invalidate_stale_content_and_content_save_does_not_reopen_completed_task(self):
        stale = self.token(self.writer)
        self.post(self.controller, action="note", note="Uwzględnij zmianę")
        self.client.force_login(self.writer)
        self.assertEqual(
            self.client.post(
                self.url, {"_edit_version": stale, "action": "content", "content": "Stara wersja"}
            ).status_code,
            409,
        )
        self.post(self.manager, action="stage", stage="completed")
        self.post(self.writer, action="content", content="Ostateczna treść")
        self.task.refresh_from_db()
        self.description.refresh_from_db()
        self.assertEqual(self.task.status, "ready")
        self.assertEqual(self.description.stage, "completed")

    def test_bulk_note_edit_invalidates_parent_form(self):
        self.post(self.controller, action="note", note="Pierwsza treść")
        previous = version_of(self.book)
        self.description.notes.update(content="Zmieniona uwaga")
        self.assertGreater(version_of(self.book), previous)

    def test_revoked_controller_cannot_save_and_unknown_action_is_bad_request(self):
        self.description.controllers.clear()
        self.assertEqual(
            self.post(self.controller, action="note", note="Brak dostępu").status_code, 403
        )
        self.assertEqual(self.post(self.writer, action="unknown").status_code, 400)
        self.assertFalse(AudioDescriptionNote.objects.exists())

    def test_list_labels_hide_completed_and_detail_has_independent_forms(self):
        self.post(self.writer, action="stage", stage="completed")
        url = reverse("core:audio_descriptions")
        response = self.client.get(url)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertFalse(response.context["hide_completed"])
        self.assertContains(response, "<th>Kto pisze</th>")
        self.assertContains(response, "<th>Konsultacja</th>")
        self.assertEqual(
            self.client.get(url, {"hide_completed": "1"}).context["page_obj"].paginator.count, 0
        )
        self.client.force_login(self.manager)
        doc = html.fromstring(self.client.get(self.url).content)
        self.assertEqual(len(doc.xpath('//div[@class="ad-columns"]/section')), 2)
        self.assertEqual(
            doc.xpath('//form/input[@name="action"]/@value'),
            ["assignment", "stage", "content", "note"],
        )
        self.assertTrue(
            doc.xpath('//select[@name="controllers"][@multiple][@data-person-multiple]')
        )

    def test_admin_content_stage_and_note_history_and_collapsed_dashboard(self):
        self.post(self.controller, action="note", note="Historia w adminie")
        note = self.description.notes.get()
        self.client.force_login(self.admin)
        url = reverse("admin:core_audiodescription_change", args=[self.description.pk])
        page = self.client.get(url)
        self.assertContains(page, "Historia w adminie")
        self.assertContains(page, 'data-person-multiple="true"')
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        data = {
            "_edit_version": token,
            "stage": "completed",
            "content": "Treść z admina",
            "controllers": [self.controller.person_profile.pk],
            "notes-TOTAL_FORMS": "1",
            "notes-INITIAL_FORMS": "1",
            "notes-0-id": str(note.pk),
            "notes-0-description": str(self.description.pk),
            "_save": "Zapisz",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.description.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.description.content, "Treść z admina")
        self.assertEqual(self.task.status, "ready")
        dashboard = html.fromstring(self.client.get(reverse("admin:index")).content)
        self.assertTrue(dashboard.xpath('//div[contains(@class,"cms-admin-menu-columns")]'))
        self.assertTrue(dashboard.xpath('//aside[@class="cms-admin-recent"]/details[not(@open)]'))


class MultiPersonLookupTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "Test JavaScript wymaga Node.js.")
    def test_multiple_selection_search_and_removal(self):
        script = Path(__file__).with_name("js_tests") / "person_multiple.cjs"
        result = subprocess.run(
            [shutil.which("node"), str(script)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
