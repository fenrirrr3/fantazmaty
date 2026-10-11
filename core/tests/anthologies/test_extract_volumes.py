"""Tomy Ekstraktów: import tomów i stopek, jeden tekst na tom, konwersja dawnych miniatur, flaga antologii i strony tomów."""
import io
import json
import tempfile
from datetime import date
from pathlib import Path

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.forms.models import model_to_dict
from django.test import TestCase, RequestFactory
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.models import PostLayoutAssignment
from core.selectors.people import profile_assignments
from core.supervision import anthology_credit_groups, integrity_issues
from fantazmaty.testing import create_member, create_superuser
from people.models import Person
from texts.admin import AnthologyAdmin, ExtractVolumeCreditInline, TextAdminForm
from texts.extract_whole import SOURCE, ensure_whole_text
from texts.models import (
    Anthology,
    AnthologyTask,
    Extract,
    ExtractTextLink,
    ExtractVolume,
    ExtractVolumeCredit,
    Text,
)
from workflow.models import WorkflowStage

from .support import AnthologyPageData


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
            self.assertEqual(text.title, text.anthology.title)
            self.assertEqual(list(text.authors.all()), [])
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
        self.assertEqual(text.title, text.anthology.title)
        self.sources[2].accepted_titles = ""
        self.sources[2].save()
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
        self.assertEqual(len(rows), 2)
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
        self.client.force_login(create_superuser())
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
        self.assertEqual(len(rows), 2)

    def test_existing_unlinked_anthology_aborts(self):
        Anthology.objects.create(title="Ekstrakty 2", is_extracts=True)
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertEqual(Text.objects.count(), 1)
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
            Text.objects.get(anthology__title="Ekstrakty 1").title, "Ekstrakty 1"
        )


class WholeExtractVolumeTests(TestCase):
    def test_future_volumes_are_single_workflow_ready_for_assignment(self):
        book = Anthology.objects.create(title='Ekstrakty 4', is_extracts=True)
        text = book.texts.get()
        self.assertEqual(text.title, book.title)
        self.assertEqual(text.workflow_stages.get().stage_type, 'ready_for_editing')
        self.assertFalse(text.authors.exists())
        self.assertFalse(text.workflow_role_assignments.exists())
        self.assertIsNone(text.length)
        text.full_clean()
        book.save()
        self.assertEqual(book.texts.count(), 1)
        with self.assertRaises(ValidationError):
            Text.objects.create(title='Miniatura', length=1, anthology=book)
        form = TextAdminForm(data=model_to_dict(text), instance=text)
        self.assertTrue(form.is_valid(), form.errors)
        self.client.force_login(create_member('extract-editor', 'Redaktor'))
        response = self.client.get(reverse('core:available_texts'))
        self.assertContains(response, book.title)
        for name in ('core:text_list', 'core:workflow_list'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, book.title)
        with self.assertRaises(ValidationError):
            text.anthology = Anthology.objects.create(title='Inna książka')
            text.save(update_fields=['anthology'])
        text.refresh_from_db()
        book.title = 'Ekstrakty 4 – dopisek'
        book.save(update_fields=['title'])
        text.refresh_from_db()
        self.assertEqual(text.title, book.title)

    def test_flag_replaces_title_detection_and_excludes_novels_and_existing_texts(self):
        named = Anthology.objects.create(title='Ekstrakty jesienne bez flagi')
        self.assertFalse(Text.objects.filter(anthology=named).exists())
        flagged = Anthology.objects.create(title='Zbiorówka', is_extracts=True)
        whole = Text.objects.get(anthology=flagged)
        self.assertTrue(whole.is_extract_volume)
        flagged.is_novel = True
        with self.assertRaises(ValidationError):
            flagged.full_clean()
        busy = Anthology.objects.create(title='Zwykła')
        Text.objects.create(title='Zwykły tekst', anthology=busy, length=5)
        busy.is_extracts = True
        with self.assertRaises(ValidationError):
            busy.full_clean()

    def test_first_two_ready_third_available_without_fake_dates(self):
        for n in (1, 2, 3):
            book = Anthology.objects.create(title=f'Ekstrakty {n}', is_extracts=True)
            book.refresh_from_db()
            stage = book.texts.get().workflow_stages.get()
            self.assertEqual(book.status, 'ready' if n < 3 else 'in_preparation')
            self.assertEqual(stage.stage_type, 'ready' if n < 3 else 'ready_for_editing')
            self.assertIsNone(stage.started_at)
            self.assertIsNone(stage.ended_at)

    def test_extracts_texts_without_author_are_not_reported(self):
        """Błąd: kontrola spójności zgłaszała teksty tomów Ekstraktów jako „Tekst bez autora”."""
        regular = Anthology.objects.create(title='Zwykła antologia')
        Text.objects.create(title='Bez autora zwykły', anthology=regular, length=1)
        for number in (1, 2, 3):
            book = Anthology.objects.create(title=f'Ekstrakty {number}')
            Text.objects.create(title=f'Ekstrakt {number}', anthology=book, length=1)
        found = {item['detail'] for item in integrity_issues() if item['label'] == 'Tekst bez autora'}
        self.assertIn('Bez autora zwykły', found)
        self.assertFalse({'Ekstrakt 1', 'Ekstrakt 2', 'Ekstrakt 3'} & found)

    def legacy(self, n, count=2):
        author = Author.objects.create(first_name='Autor', last_name=str(n))
        book = Anthology.objects.create(title=f'Dawny tom {n}')
        source = Extract.objects.create(author=author, full_name=str(author), recruitment=f'Ekstrakty {n}',
                                        title='Pierwszy\nDrugi', accepted_titles='Pierwszy\nDrugi')
        for name in ['Pierwszy', 'Drugi'][:count]:
            text = Text.objects.create(title=name, anthology=book, import_source='extracts-v1')
            text.authors.add(author)
            WorkflowStage.objects.create(text=text, stage_type='ready' if n < 3 else 'ready_for_editing',
                                         is_current=True, is_released=True)
            ExtractTextLink.objects.create(text=text, extract=source, title_key=name.casefold(), source_title=name)
        Anthology.objects.filter(pk=book.pk).update(title=f'Ekstrakty {n}', is_extracts=True)
        book.refresh_from_db()
        return book, source

    def run_conversion(self, apply=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            call_command('consolidate_extract_volumes', apply=apply, report=str(path), stdout=io.StringIO())
            return json.loads(path.read_text(encoding='utf8'))

    def test_conversion_preview_apply_and_repeat_preserve_information(self):
        book, source = self.legacy(1)
        original_source = Extract.objects.values().get(pk=source.pk)
        original_authors = list(Author.objects.values())
        self.run_conversion()
        self.assertEqual(book.texts.count(), 2)
        report = self.run_conversion(True)
        self.assertEqual(report['volumes'][0]['removed_generated_miniatures'], 2)
        self.assertEqual(len(report['volumes'][0]['snapshots']), 2)
        text = book.texts.get()
        self.assertEqual(text.import_source, SOURCE)
        self.assertEqual(text.workflow_stages.get().stage_type, 'ready')
        self.assertEqual(Extract.objects.values().get(pk=source.pk), original_source)
        self.assertEqual(list(Author.objects.values()), original_authors)
        self.assertFalse(ExtractTextLink.objects.exists())
        self.run_conversion(True)
        self.assertEqual(book.texts.get().pk, text.pk)
        self.assertEqual(ensure_whole_text(book).pk, text.pk)

    def test_conversion_rolls_back_all_volumes_on_actual_work(self):
        book, _ = self.legacy(1)
        next_book, _ = self.legacy(3)
        stage = next_book.texts.first().workflow_stages.get()
        stage.started_at = date(2026, 8, 1)
        stage.save()
        with self.assertRaises(CommandError):
            self.run_conversion(True)
        self.assertEqual(book.texts.count(), 2)
        self.assertEqual(next_book.texts.count(), 2)
        self.assertEqual(ExtractTextLink.objects.count(), 4)


class ExtractVolumePageTests(AnthologyPageData, TestCase):
    def test_extract_profile_only_text_roles_and_hidden_sections_for_volumes_one_two(self):
        person = self.member.person_profile
        permitted = ("Redakcja", "Korekta", "Weryfikacja", "Korekta poskładowa")
        for number in (1, 2, 3):
            book = Anthology.objects.create(title=f"Ekstrakty {number}")
            ExtractVolume.objects.create(anthology=book, number=number)
            for role in (*permitted, "Koordynacja działu redakcji", "Redaktor prowadzący", "Skład",
                         "Audiodeskrypcja"):
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

    def test_extract_panels_and_length_only_change_whole_volumes(self):
        self.client.force_login(self.admin)
        normal_text = Text.objects.create(title="Testowy tekst", anthology=self.book, length=100)
        whole = ensure_whole_text(Anthology.objects.create(title="Ekstrakty 3", is_extracts=True))
        page = self.client.get(reverse("core:assigned_text_detail", args=[whole.pk]))
        doc = html.fromstring(page.content)
        headings = [x.text_content().strip() for x in doc.xpath("//h2|//summary")]
        for label in ["Recenzje tekstu", "Audiobook", "Ilustracja", "Wycofaj tekst", "Tagi i gatunek"]:
            self.assertFalse(any(label in value for value in headings), label)
        self.assertContains(page, "Notatki zespołu")
        self.assertContains(page, "nie dotyczy")
        page = self.client.get(reverse("core:text_list"), {"q": "Ekstrakty"})
        self.assertContains(page, "n.d.")
        self.assertNotContains(page, ">None<")
        normal = self.client.get(reverse("core:assigned_text_detail", args=[normal_text.pk]))
        self.assertContains(normal, "Recenzje tekstu")
        self.assertContains(normal, "Tagi i gatunek")
