import io
import json
import tempfile
from pathlib import Path

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase, RequestFactory
from django.urls import reverse

from authors.models import Author
from core.models import PostLayoutAssignment
from core.selectors.people import profile_assignments
from core.supervision import anthology_credit_groups
from people.models import Person
from texts.admin import AnthologyAdmin, ExtractVolumeCreditInline, TextAdminForm
from texts.models import (
    Anthology,
    AnthologyTask,
    Extract,
    ExtractTextLink,
    ExtractVolume,
    ExtractVolumeCredit,
    Text,
)


class ExtractVolumeImportTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.person = Person.objects.create(first_name="Anna", last_name="Próbna", is_active=False)
        self.author = Author.objects.create(
            first_name="Autor", last_name="Próbny", email="author@example.com"
        )
        self.sources = []
        for number in (1, 2, 3):
            self.sources.append(
                Extract.objects.create(
                    author=self.author,
                    full_name="Autor Próbny",
                    email="author@example.com",
                    recruitment=f"Ekstrakty {number}",
                    title="Tak, przecinek\nNie\nBez decyzji",
                    accepted_titles="Tak, przecinek",
                    rejected_titles="Nie",
                )
            )
        self.payload = {
            "schema": "extract-volumes-v1",
            "volumes": [
                {
                    "number": 1,
                    "credits": [
                        {"role": "Redakcja", "name": "Anna Próbna"},
                        {"role": "Skład", "name": "Anna Próbna", "task": "typesetting"},
                        {"role": "Okładka", "name": "Anna Próbna", "cover": True},
                    ],
                },
                {"number": 2, "credits": [{"role": "Korekta poskładowa", "name": "Anna Próbna"}]},
                {"number": 3, "credits": []},
            ],
        }

    def run_import(self, apply=False, **kwargs):
        data = self.path / "data.json"
        data.write_text(json.dumps(self.payload), encoding="utf8")
        call_command(
            "import_extract_volumes",
            str(data),
            apply=apply,
            report=str(self.path / "report.json"),
            stdout=io.StringIO(),
            **kwargs,
        )
        return json.loads((self.path / "report.json").read_text(encoding="utf8"))

    def test_preview_apply_repeat_preserves_sources_and_authors(self):
        original = list(Extract.objects.values())
        report = self.run_import()
        self.assertEqual(len(report["texts"]), 3)
        self.assertFalse(Text.objects.exists())
        self.assertFalse(Anthology.objects.exists())
        self.run_import(True)
        self.assertEqual(Text.objects.count(), 3)
        self.assertEqual(ExtractVolume.objects.count(), 3)
        self.assertEqual(ExtractVolumeCredit.objects.count(), 4)
        for text in Text.objects.all():
            self.assertEqual(text.title, "Tak, przecinek")
            self.assertEqual(list(text.authors.all()), [self.author])
            self.assertIsNone(text.length)
            self.assertIsNone(text.workflow_stages.get().started_at)
        self.assertEqual(list(Extract.objects.values()), original)
        self.assertEqual(
            [v["unchanged_texts"] for v in self.run_import(True)["volumes"]], [1, 1, 1]
        )
        self.assertEqual(Text.objects.count(), 3)

    def test_statuses_tasks_and_no_fake_miniature_assignments(self):
        self.run_import(True)
        for volume in ExtractVolume.objects.select_related("anthology"):
            book = volume.anthology
            self.assertEqual(book.status, "ready" if volume.number < 3 else "in_preparation")
            text = book.texts.get()
            self.assertEqual(
                text.workflow_stages.get().stage_type,
                "ready" if volume.number < 3 else "ready_for_editing",
            )
            self.assertFalse(text.workflow_role_assignments.exists())
        book = Anthology.objects.get(title="Ekstrakty 1")
        self.assertEqual(book.cover_author, "Anna Próbna")
        task = book.production_tasks.get(task_type="typesetting")
        self.assertEqual(
            (task.assigned_to_id, task.status, task.commissioned_at),
            (self.person.pk, "ready", None),
        )
        self.assertEqual(book.production_tasks.get(task_type="blurb").status, "not_commissioned")

    def test_unknown_or_duplicate_person_rolls_everything_back(self):
        self.payload["volumes"][1]["credits"].append({"role": "Korekta", "name": "Nieznana Osoba"})
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertFalse(Anthology.objects.exists())
        self.payload["volumes"][1]["credits"].pop()
        Person.objects.create(first_name="Anna", last_name="Próbna")
        with self.assertRaises(CommandError):
            self.run_import(True, create_missing_people=True)
        self.assertFalse(Anthology.objects.exists())

    def test_explicit_profile_creation_without_accounts_or_roles(self):
        self.payload["volumes"][1]["credits"].append({"role": "Korekta", "name": "Nieznana Osoba"})
        self.run_import(create_missing_people=True)
        self.assertFalse(Person.objects.filter(first_name="Nieznana").exists())
        self.run_import(True, create_missing_people=True)
        person = Person.objects.get(first_name="Nieznana")
        self.assertFalse(person.is_active)
        self.assertIsNone(person.email)
        self.assertIsNone(person.user_id)
        self.assertFalse(person.roles.exists())

    def test_conflict_late_in_import_rolls_back_texts_and_tasks(self):
        self.sources[2].accepted_titles = "A" * 256
        self.sources[2].title = "A" * 256
        self.sources[2].rejected_titles = ""
        self.sources[2].save()
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertFalse(Text.objects.exists())
        self.assertFalse(AnthologyTask.objects.exists())

    def test_rerun_never_restores_removed_acceptance_or_steals_text(self):
        self.run_import(True)
        text = Text.objects.get(anthology__title="Ekstrakty 3")
        text.title = "Poprawiony tytuł"
        text.save()
        self.run_import(True)
        text.refresh_from_db()
        self.assertEqual(text.title, "Poprawiony tytuł")
        self.sources[2].accepted_titles = ""
        self.sources[2].save()
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertEqual(Text.objects.count(), 3)

    def test_regular_text_length_still_required_and_admin_can_edit_miniature(self):
        with self.assertRaises(ValidationError):
            Text(title="Zwykły tekst", length=None).full_clean()
        self.run_import(True)
        text = Text.objects.first()
        self.assertFalse(TextAdminForm(instance=text).fields["length"].required)
        text.full_clean()

    def test_volume_history_without_account_and_ordinary_book_unchanged(self):
        self.run_import(True)
        rows, summary = profile_assignments(self.person, include_authors=True)
        self.assertEqual(len(rows), 4)
        self.assertEqual(summary["completed"], 2)
        self.assertTrue(all(row["text"]["authors"]["all"] == [] for row in rows))
        self.assertTrue(all(row["has_completed_work"] for row in rows))
        book = Anthology.objects.get(title="Ekstrakty 1")
        self.assertEqual(len(anthology_credit_groups(book)), 3)
        ordinary = Anthology.objects.create(title="Inna")
        self.assertEqual(len(anthology_credit_groups(ordinary)), 8)
        site_admin = AnthologyAdmin(Anthology, admin.site)
        request = RequestFactory().get("/")
        self.assertIn(ExtractVolumeCreditInline, site_admin.get_inlines(request, book))
        self.assertNotIn(ExtractVolumeCreditInline, site_admin.get_inlines(request, ordinary))
        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "test")
        self.client.force_login(user)
        response = self.client.get(reverse("core:person_detail", args=[self.person.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Brak przypisanego autora")
        self.assertContains(response, "Ekstrakty 1")
        self.assertEqual(
            self.client.get(reverse("core:anthology_detail", args=[book.pk])).status_code, 200
        )

    def test_post_layout_import_does_not_duplicate_volume_history(self):
        user = get_user_model().objects.create_user(
            "proofreader", first_name="Anna", last_name="Próbna"
        )
        self.person.user = user
        self.person.save()
        self.run_import(True)
        book = Anthology.objects.get(title="Ekstrakty 2")
        PostLayoutAssignment.objects.create(
            anthology=book,
            proofreader=user,
            historical=True,
            status="completed",
            page_from=None,
            page_to=None,
            assigned_start=None,
            created_by=None,
        )
        rows, _ = profile_assignments(self.person, include_authors=True)
        self.assertEqual(len(rows), 4)

    def test_existing_unlinked_anthology_aborts(self):
        Anthology.objects.create(title="Ekstrakty 2")
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertFalse(Text.objects.exists())
        self.assertFalse(ExtractTextLink.objects.exists())

    def test_explicit_mapping_of_person_and_recruitment(self):
        self.person.first_name = "Inne imię"
        self.person.save()
        self.sources[0].recruitment = "Dawny nabór"
        self.sources[0].save()
        mapping = self.path / "mapping.json"
        mapping.write_text(
            json.dumps(
                {"people": {"Anna Próbna": self.person.pk}, "recruitments": {"Dawny nabór": 1}}
            ),
            encoding="utf8",
        )
        self.run_import(True, mapping=str(mapping))
        self.assertEqual(ExtractVolumeCredit.objects.filter(person=self.person).count(), 4)
        self.assertEqual(
            ExtractTextLink.objects.get(extract=self.sources[0]).text.anthology.title, "Ekstrakty 1"
        )
