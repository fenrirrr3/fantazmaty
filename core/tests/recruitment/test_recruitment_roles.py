"""Role kandydatów w rekrutacji: edycja ról w adminie i starym formularzu, zachowanie decyzji, notatek i kontrola wersji."""
from django.test import TestCase
from django.urls import reverse

from core.models import Recruitment
from core.selectors.recruitment import pending_by_role
from fantazmaty.testing import create_superuser, form_value


class RecruitmentRoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('repair-admin')

    def setUp(self):
        self.client.force_login(self.admin)

    def token(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return form_value(response)

    def test_admin_roles_sync_archive_notes_reactivate_and_reject_stale_role_form(self):
        record = Recruitment.objects.create(
            first_name="Jan",
            last_name="Kandydat",
            email="jan@example.test",
            mail_roles=["editors"],
            mail_fingerprint="a" * 64,
        )
        decision = record.role_decisions.get(role="editors")
        decision.status = "accepted"
        decision.decision_reason = "Wcześniejsze uzasadnienie"
        decision.unofficial_notes = "<script>prywatne</script>"
        decision.save()
        record.save()
        old_version = decision.updated_at.isoformat()
        url = reverse("admin:core_recruitment_change", args=[record.pk])
        payload = {
            "first_name": "Jan",
            "last_name": "Kandydat",
            "email": record.email,
            "submitted_at": "2026-10-08",
            "_save": "Zapisz",
        }
        page = self.client.get(url)
        self.assertNotIn("department", page.context["adminform"].form.fields)
        for roles in (["proofreaders", "promotion"], ["editors", "proofreaders", "sound"]):
            response = self.client.post(
                url, {**payload, "selected_roles": roles, "_edit_version": self.token(url)}
            )
            self.assertEqual(response.status_code, 302)
            record.refresh_from_db()
            self.assertCountEqual(record.mail_roles, roles)
            self.assertEqual(record.status, "new")
            self.assertEqual(
                record.role_decisions.get(role="editors").decision_reason,
                "Wcześniejsze uzasadnienie",
            )
            if "editors" not in roles:
                page = self.client.get(url)
                self.assertContains(page, "Wcześniejsze uzasadnienie")
                self.assertContains(page, "&lt;script&gt;prywatne&lt;/script&gt;")
                self.assertEqual(
                    {r["role"] for r in pending_by_role()}, {"proofreaders", "promotion"}
                )
        self.assertEqual(record.role_decisions.get(role="editors").status, "accepted")
        self.assertIn("Dźwiękowcy", record.mail_roles_display)
        detail = reverse("core:recruitment_detail", args=[record.pk])
        self.assertEqual(
            self.client.post(
                detail, {"role": "editors", "version": old_version, "editors-status": "rejected"}
            ).status_code,
            409,
        )
        page = self.client.get(
            reverse("admin:core_recruitment_changelist"), {"recruitment_role": "sound"}
        )
        self.assertEqual([row.pk for row in page.context["cl"].result_list], [record.pk])
        self.assertContains(page, "Dźwiękowcy")
        response = self.client.post(url, {**payload, "_edit_version": self.token(url)})
        self.assertEqual(response.status_code, 200)
        self.assertIn("selected_roles", response.context["adminform"].form.errors)
        record.refresh_from_db()
        self.assertCountEqual(record.mail_roles, ["editors", "proofreaders", "sound"])

    def test_admin_and_page_add_multiple_roles_and_reject_invalid_roles(self):
        """Dodanie zgłoszenia w adminie i na stronie (core:recruitment_add): kilka ról naraz, nieznana rola odrzuca całość."""
        payload = {
            "first_name": "Nowa",
            "last_name": "Osoba",
            "email": "new@example.test",
            "submitted_at": "2026-10-08",
        }
        for url, extra, invalid_status, target in (
            (reverse("admin:core_recruitment_add"), {"_save": "Zapisz"}, 200, None),
            (reverse("core:recruitment_add"), {}, 400, reverse("core:recruitment_list")),
        ):
            with self.subTest(url=url):
                Recruitment.objects.all().delete()
                self.assertEqual(self.client.get(url).status_code, 200)
                response = self.client.post(url, {**payload, **extra, "selected_roles": ["editors", "fake"]})
                self.assertEqual(response.status_code, invalid_status)
                self.assertFalse(Recruitment.objects.exists())
                response = self.client.post(url, {**payload, **extra, "selected_roles": ["editors", "illustrators"]})
                self.assertEqual(response.status_code, 302)
                if target:
                    self.assertEqual(response.url, target)
                record = Recruitment.objects.get()
                self.assertCountEqual(
                    record.role_decisions.values_list("role", flat=True), ["editors", "illustrators"]
                )
                self.assertTrue(all(row.status == "new" for row in record.role_decisions.all()))

    def test_legacy_edit_url_changes_actual_roles_and_checks_version(self):
        record = Recruitment.objects.create(
            first_name="Jan",
            last_name="Kandydat",
            email="jan@example.test",
            mail_roles=["editors"],
            mail_fingerprint="b" * 64,
        )
        url = reverse("core:recruitment_edit", args=[record.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("department", page.context["form"].fields)
        payload = {
            "first_name": "Jan",
            "last_name": "Kandydat",
            "email": record.email,
            "submitted_at": "2026-10-08",
            "selected_roles": ["proofreaders", "illustrators"],
            "version": record.updated_at.isoformat(),
        }
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.mail_roles, ["proofreaders", "illustrators"])
        self.assertCountEqual(
            record.role_decisions.values_list("role", flat=True),
            ["editors", "proofreaders", "illustrators"],
        )
        self.assertEqual(self.client.post(url, payload).status_code, 409)
