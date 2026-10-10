import os
import subprocess
import sys

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.models import AudioDescription
from texts.models import Anthology
from workflow.tests import create_member


class AudioDescriptionTests(TestCase):
    def setUp(self):
        self.member = create_member("describer", "Redaktor")
        self.other = create_member("controller", "Korektor")
        self.manager = create_member("manager", "Koordynator redakcji")
        self.book = Anthology.objects.create(title="Antologia do opisania")
        self.task = self.book.production_tasks.get(task_type="audio_description")
        self.description = self.book.audio_description
        self.list_url = reverse("core:audio_descriptions")
        self.detail = reverse("core:audio_description_detail", args=[self.book.pk])
        self.claim = reverse("core:claim_audio_description", args=[self.book.pk])

    def token(self, user):
        return signing.dumps(
            [user.pk, f"texts.anthology:{self.book.pk}", version_of(self.book)],
            salt="cms-edit-version",
        )

    def post(self, user, data, url=None):
        self.client.force_login(user)
        return self.client.post(url or self.detail, {**data, "_edit_version": self.token(user)})

    def test_claim_from_rendered_form_updates_existing_task(self):
        self.client.force_login(self.member)
        before = AudioDescription.objects.count()
        page = self.client.get(self.list_url)
        form = html.fromstring(page.content).xpath(f'//form[@action="{self.claim}"]')[0]
        values = {i.get("name"): i.get("value") for i in form.xpath(".//input")}
        self.assertEqual(self.client.post(self.claim, values).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(
            (self.task.assigned_to_id, self.task.status),
            (self.member.person_profile.pk, "commissioned"),
        )
        self.assertIsNotNone(self.task.commissioned_at)
        self.assertEqual(
            self.book.production_tasks.filter(task_type="audio_description").count(), 1
        )
        self.assertEqual(AudioDescription.objects.count(), before)
        # The task status is managed on the anthology page; the writer moves the work stage.
        detail = self.client.get(self.detail)
        self.assertNotContains(detail, 'name="status"')
        self.assertContains(detail, 'name="stage"')

    def test_stale_second_claim_and_ready_task_never_overwrite(self):
        stale = self.token(self.other)
        self.post(self.member, {}, self.claim)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.claim, {"_edit_version": stale}).status_code, 409)
        self.post(self.other, {}, self.claim)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.member.person_profile.pk)
        self.post(self.member, {"action": "stage", "stage": "completed"})
        self.post(self.other, {}, self.claim)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "ready")

    def test_coordinator_assigns_controller_and_only_designated_users_edit(self):
        self.assertEqual(self.post(self.other, {"action": "stage", "stage": "completed"}).status_code, 403)
        self.assertEqual(
            self.post(
                self.manager,
                {
                    "assigned_to": self.member.person_profile.pk,
                    "controllers": [self.other.person_profile.pk],
                    "status": "ready",
                },
            ).status_code,
            302,
        )
        self.task.refresh_from_db()
        # The status sent from this page is ignored: assignment only commissions the task.
        self.assertEqual(self.task.status, "commissioned")
        self.assertEqual(self.post(self.other, {"action": "assignment", "assigned_to": ""}).status_code, 403)
        self.assertEqual(self.post(self.other, {"action": "stage", "stage": "completed"}).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "ready")
        self.description.controllers.clear()
        self.assertEqual(self.post(self.other, {"action": "note", "note": "x"}).status_code, 403)

    def test_assignee_cannot_change_owner_or_grant_control(self):
        self.post(self.member, {}, self.claim)
        self.post(
            self.member,
            {
                "status": "ready",
                "assigned_to": self.other.person_profile.pk,
                "controllers": [self.other.person_profile.pk],
            },
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.member.person_profile.pk)
        self.assertFalse(self.description.controllers.exists())

    def test_controller_without_assignee_gets_validation_error(self):
        self.description.controllers.add(self.other.person_profile)
        response = self.post(self.other, {"action": "stage", "stage": "completed"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "wymaga przypisania osoby piszącej", status_code=400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "not_commissioned")

    def test_controller_assignment_protects_anthology_from_admin_delete(self):
        from django.contrib import admin
        from django.core.exceptions import PermissionDenied
        from django.test import RequestFactory

        self.description.controllers.add(self.other.person_profile)
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("delete-admin", "", "test")
        model_admin = admin.site.get_model_admin(Anthology)
        self.assertIn(
            AudioDescription._meta.verbose_name,
            model_admin.get_deleted_objects([self.book], request)[2],
        )
        with self.assertRaises(PermissionDenied):
            model_admin.delete_model(request, self.book)
        self.assertTrue(Anthology.objects.filter(pk=self.book.pk).exists())

    def test_inactive_external_or_no_profile_cannot_claim(self):
        person = self.member.person_profile
        person.is_external = True
        person.save()
        response = self.post(self.member, {}, self.claim)
        self.assertIn(response.status_code, (302, 403))
        if response.status_code == 302:
            self.assertIn(reverse("login"), response.url)
        user = get_user_model().objects.create_user("accountonly")
        self.assertEqual(self.post(user, {}, self.claim).status_code, 403)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to_id)

    def test_removed_assignment_revokes_access_even_with_old_token(self):
        self.post(self.member, {}, self.claim)
        stale = self.token(self.member)
        self.task.assigned_to = self.other.person_profile
        self.task.save()
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.post(self.detail, {"status": "ready", "_edit_version": stale}).status_code,
            403,
        )

    def test_admin_controller_change_invalidates_old_forms(self):
        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "test")
        old = self.token(self.manager)
        self.client.force_login(admin)
        url = reverse("admin:core_audiodescription_change", args=[self.description.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(
            url,
            {
                "controllers": [self.other.person_profile.pk],
                "stage": "writing",
                "content": "",
                "notes-TOTAL_FORMS": "0",
                "notes-INITIAL_FORMS": "0",
                "_save": "Zapisz",
                "_edit_version": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            self.description.controllers.filter(pk=self.other.person_profile.pk).exists()
        )
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.post(
                self.detail, {"status": "not_commissioned", "_edit_version": old}
            ).status_code,
            409,
        )

    def test_filters_sorting_and_no_writes_on_get(self):
        second = Anthology.objects.create(title="Zielona antologia")
        Anthology.objects.create(title="Porzucona", status="abandoned")
        Anthology.objects.create(title="Powieść", is_novel=True)
        self.client.force_login(self.member)
        count = AudioDescription.objects.count()
        response = self.client.get(self.list_url, {"sort": "-anthology"})
        self.assertEqual(response.context["rows"][0]["book"], second)
        self.assertNotContains(response, "Porzucona")
        self.assertNotContains(response, "Powieść</a>")
        response = self.client.get(
            self.list_url, {"q": "do opisania", "status": "not_commissioned"}
        )
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual(AudioDescription.objects.count(), count)
        self.assertEqual(self.client.get(self.claim).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(self.list_url).status_code, 302)

    def test_missing_version_rejected_and_invalid_status_not_saved(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(self.claim, {}).status_code, 409)
        self.assertEqual(
            self.post(self.manager, {"status": "ready", "assigned_to": ""}).status_code, 302
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "not_commissioned")


class MySQLSettingsBootstrapTests(SimpleTestCase):
    def test_ci_settings_import_without_production_environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
        env.update(TEST_MYSQL_USER="isolated_test", TEST_MYSQL_PASSWORD="isolated_test")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                'from fantazmaty.mysql_integration_settings import DATABASES; assert DATABASES["default"]["ENGINE"] == "django.db.backends.mysql"; print("OK")',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "OK")
