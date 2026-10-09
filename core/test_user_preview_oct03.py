"""Focused security and workflow visibility tests for the read-only preview."""
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY as AUTH_SESSION_KEY, get_user_model
from django.test import Client
from django.urls import reverse

from core.test_status_assignment_regression import StatusAssignmentFixtures
from core.user_preview import SESSION_KEY
from people.models import Role
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S


class UserPreviewTests(StatusAssignmentFixtures):
    def setUp(self):
        super().setUp()
        self.activity = patch("core.activity_spool.enqueue_activity").start()
        self.addCleanup(patch.stopall)
        self.client.force_login(self.admin)
        self.start_url = reverse("core:user_preview")
        self.stop_url = reverse("core:user_preview_stop")
        self.member.person_profile.roles.add(Role.objects.get_or_create(name="Redaktor")[0])
        assignment = A.objects.create(text=self.text, role="editor", assigned_to=self.member)
        self.editing = S.objects.create(text=self.text, stage_type="editing", assignment=assignment,
                                       started_at=self.today)
        verification = A.objects.create(text=self.text, role="verifier_1", assigned_to=self.other)
        self.verification = S.objects.create(text=self.text, stage_type="first_verification",
                                            assignment=verification)

    def preview(self, target=None):
        return self.client.post(self.start_url, {"target": (target or self.member).pk})

    def test_searchable_picker_and_links_are_superuser_only(self):
        response = self.client.get(self.start_url)
        self.assertContains(response, 'data-searchable-select')
        self.assertContains(response, self.member.email)
        self.assertNotContains(response, f'value="{self.admin.pk}"')
        self.assertContains(self.client.get(reverse("core:home")), self.start_url)
        self.assertContains(self.client.get(reverse("admin:index")), self.start_url)
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse("core:home")), self.start_url)
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(self.start_url, {"target": self.other.pk}).status_code, 403)
        self.assertEqual(self.client.post(self.stop_url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.start_url).status_code, 403)

    def test_preview_keeps_admin_session_and_does_not_log_in_target(self):
        self.member.refresh_from_db()
        before_login = self.member.last_login
        before_hash = self.member.password
        self.assertRedirects(self.preview(), reverse("core:home"))
        self.assertEqual(self.client.session[AUTH_SESSION_KEY], str(self.admin.pk))
        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.wsgi_request.user.pk, self.member.pk)
        self.assertEqual(response.wsgi_request.preview_actor.pk, self.admin.pk)
        self.assertContains(response, "Podgląd jako: Jan Test")
        self.assertContains(response, "Wróć do administratora")
        self.assertContains(response, self.text.title)
        self.assertNotContains(response, "Panel administracyjny")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.last_login, before_login)
        self.assertEqual(self.member.password, before_hash)

    def test_personal_views_and_verification_buttons_match_actual_user(self):
        actual = Client()
        actual.force_login(self.member)
        detail = reverse("core:assigned_text_detail", args=[self.text.pk])
        ordinary = actual.get(detail)
        self.assertContains(ordinary, "Przekaż do pierwszej weryfikacji")
        self.preview()
        response = self.client.get(detail)
        self.assertContains(response, "Przekaż do pierwszej weryfikacji")
        self.assertNotContains(response, "Przekaż do drugiej weryfikacji")
        self.assertContains(self.client.get(reverse("core:my_texts")), self.text.title)
        dashboard = self.client.get(reverse("core:home"))
        self.assertEqual(dashboard.context["active_stage_count"], actual.get(reverse("core:home")).context["active_stage_count"])
        self.assertEqual(self.client.get(reverse("core:author_list")).status_code, 403)

    def test_workflow_and_account_writes_blocked_before_execution(self):
        self.preview()
        paths = [reverse("core:send_to_first_verification_stage", args=[self.text.pk]),
                 reverse("password_change"), reverse("logout"), reverse("admin:index")]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.post(path, {"end_date": str(self.today)})
                self.assertContains(response, "Podgląd jest tylko do odczytu", status_code=403)
                self.assertContains(response, "Wróć do administratora", status_code=403)
        for method in (self.client.put, self.client.patch, self.client.delete):
            self.assertEqual(method(reverse("core:home")).status_code, 403)
        self.editing.refresh_from_db()
        self.verification.refresh_from_db()
        self.assertFalse(self.editing.is_completed)
        self.assertIsNone(self.verification.started_at)
        self.assertEqual(self.client.session[AUTH_SESSION_KEY], str(self.admin.pk))

    def test_admin_and_password_pages_require_exiting_preview(self):
        self.preview()
        for path in (reverse("admin:index"), reverse("password_change"), reverse("login")):
            response = self.client.get(path)
            self.assertContains(response, "zakończ podgląd", status_code=403)
        self.assertContains(self.client.get(reverse("core:author_list")), "Wybrany użytkownik nie ma dostępu", status_code=403)

    def test_switch_target_and_stop_without_changing_authenticated_user(self):
        self.preview()
        self.assertContains(self.client.get(self.start_url), "Zakończ podgląd i wróć")
        self.assertRedirects(self.preview(self.other), reverse("core:home"))
        self.assertEqual(self.client.get(reverse("core:home")).wsgi_request.user.pk, self.other.pk)
        self.assertEqual(self.client.get(self.stop_url).status_code, 405)
        self.assertIn(SESSION_KEY, self.client.session)
        self.assertRedirects(self.client.post(self.stop_url), reverse("core:home"))
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.client.get(reverse("core:home")).wsgi_request.user.pk, self.admin.pk)

    def test_inactive_missing_or_self_target_rejected(self):
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        for target in (self.other.pk, self.admin.pk, 999999, "invalid"):
            with self.subTest(target=target):
                response = self.client.post(self.start_url, {"target": target})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                self.assertNotIn(SESSION_KEY, self.client.session)

    def test_csrf_required_for_start_switch_and_stop(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.start_url, {"target": self.member.pk}).status_code, 403)
        client.get(self.start_url)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(client.post(self.start_url, {"target": self.member.pk, "csrfmiddlewaretoken": token}).status_code, 302)
        self.assertEqual(client.post(self.start_url, {"target": self.other.pk}).status_code, 403)
        self.assertEqual(client.post(self.stop_url).status_code, 403)
        self.assertIn(SESSION_KEY, client.session)
        self.assertEqual(client.post(self.stop_url, {"csrfmiddlewaretoken": token}).status_code, 302)

    def test_revoked_superuser_or_target_account_cannot_continue_or_write(self):
        self.preview()
        get_user_model().objects.filter(pk=self.admin.pk).update(is_superuser=False)
        self.assertEqual(self.client.post(reverse("core:home")).status_code, 403)
        self.assertNotIn(SESSION_KEY, self.client.session)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_superuser=True)
        self.preview()
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assertEqual(self.client.post(reverse("core:home")).status_code, 403)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_forged_session_owner_cannot_impersonate(self):
        self.client.force_login(self.member)
        session = self.client.session
        session[SESSION_KEY] = {"actor": self.admin.pk, "target": self.other.pk}
        session.save()
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 403)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_invalid_preview_state_is_cleared_without_500_or_admin_write(self):
        for state in ([], {"actor": self.admin.pk, "target": []},
                      {"actor": self.admin.pk, "target": 2**90}):
            session = self.client.session
            session[SESSION_KEY] = state
            session.save()
            self.assertEqual(self.client.post(reverse("core:home")).status_code, 403)
            self.assertNotIn(SESSION_KEY, self.client.session)

    def test_preview_activity_is_attributed_to_admin_only(self):
        self.preview()
        self.activity.reset_mock()
        session = self.client.session
        session.pop("_activity_visit", None)
        session.save()
        self.client.get(reverse("core:home"))
        self.activity.assert_called_once()
        recorded = self.activity.call_args.kwargs
        self.assertEqual(recorded["user_id"], self.admin.pk)
        self.assertEqual(recorded["actor"], self.admin.email)
        self.assertIn(f"Podgląd user_id #{self.member.pk}", recorded["action"])
