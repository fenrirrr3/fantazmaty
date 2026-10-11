"""Import archiwalnej tabeli zakończonych prac (import_completed_table) i naprawa numeracji wykonań (repair_archive_executions)."""
import copy
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, tag
from django.urls import reverse

from authors.models import Author
from people.models import Person
from texts.models import Anthology, Text
from workflow.archive_executions import SOURCE, normalized_archive
from workflow.labels import assignment_label, stage_label
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A


class CompletedTableTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/'import.json'
        self.report = Path(self.folder.name)/'report'
        self.anthology = Anthology.objects.create(title='Antologia archiwalna', status='ready')
        self.person = {'first_name':'Anna','last_name':'Testowa'}
        self.data = {'schema_version':1,'source':'table-test','texts':[
            {'source_row':1,'title':'Gotowe opowiadanie','anthology':self.anthology.title,
             'length':12345,'length_source':{'sha256':'test-only'},
             'authors':[{'first_name':'Jan','last_name':'Autor'}],
             'stages':[{'stage_type':kind,'person':dict(self.person),'source_column':'Tabela'}
                       for kind in ['editing','editing','first_verification','second_verification','fourth_verification']]}]}

    def run_import(self, apply=True):
        self.path.write_text(json.dumps(self.data),encoding='utf-8')
        call_command('import_completed_table',str(self.path),apply=apply,report=str(self.report),stdout=StringIO())
        return json.loads(self.report.with_suffix('.json').read_text(encoding='utf-8'))

    def test_preview_does_not_create_any_records(self):
        report=self.run_import(False)
        self.assertIn('podgląd',report['mode'])
        for model in (Text, Person, Author, get_user_model(), S, A):
            self.assertEqual(model.objects.count(), 0)

    def test_ready_anthology_repeated_work_blank_dates_and_no_login(self):
        self.run_import()
        text = Text.objects.get()
        self.assertEqual(text.anthology, self.anthology)
        self.assertEqual(text.length, 12345)
        self.assertEqual(
            list(
                S.objects.filter(stage_type="editing")
                .order_by("execution_number")
                .values_list("execution_number", flat=True)
            ),
            [1, 2],
        )
        self.assertEqual(A.objects.filter(role="editor").count(), 2)
        self.assertEqual(A.objects.filter(role="editor", is_current=True).count(), 1)
        self.assertEqual(
            S.objects.filter(imported_completed=True, is_completed=True).count(), 5
        )
        self.assertEqual(
            S.objects.filter(stage_type="ready", is_current=True, is_released=True).count(), 1
        )
        self.assertEqual(
            S.objects.filter(
                stage_type="fourth_verification", is_current=False, is_released=False
            ).count(),
            1,
        )
        self.assertFalse(S.objects.filter(started_at__isnull=False).exists())
        self.assertFalse(S.objects.filter(ended_at__isnull=False).exists())
        self.assertFalse(S.objects.filter(queued_at__isnull=False).exists())
        self.assertFalse(A.objects.filter(assigned_at__isnull=False).exists())
        person = Person.objects.select_related("user").get()
        self.assertIsNone(person.email)
        self.assertIsNone(Author.objects.get().email)
        self.assertEqual(person.user.email, "")
        self.assertFalse(
            person.is_active
            or person.user.is_active
            or person.user.is_staff
            or person.user.is_superuser
        )
        self.assertFalse(
            person.user.has_usable_password()
            or person.roles.exists()
            or person.user.groups.exists()
        )

    def test_repeat_is_noop_and_changed_input_is_rejected(self):
        self.run_import()
        before = list(S.objects.order_by("pk").values())
        report = self.run_import()
        self.assertEqual(report["counts"], {})
        self.assertEqual(report["texts"][0]["result"], "bez zmian")
        self.assertEqual(before, list(S.objects.order_by("pk").values()))
        self.data["texts"][0]["length"] += 1
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertEqual(Text.objects.get().length, 12345)

    def test_existing_profile_keeps_email_roles_and_account(self):
        user = get_user_model().objects.create_user(
            username="existing", email="keep@example.com", password="test", **self.person
        )
        person = Person.objects.create(**self.person, user=user, email="profile@example.com")
        self.run_import()
        person.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(person.email, "profile@example.com")
        self.assertEqual(user.email, "keep@example.com")
        self.assertTrue(user.is_active and user.has_usable_password())
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_existing_profile_without_account_gets_inactive_account(self):
        person = Person.objects.create(**self.person, email=None, is_active=True)
        self.run_import()
        person.refresh_from_db()
        self.assertTrue(person.is_active)
        self.assertFalse(person.user.is_active)
        self.assertEqual(Person.objects.count(), 1)

    def test_two_missing_people_can_both_have_empty_email(self):
        self.data["texts"][0]["stages"][1]["person"]["first_name"] = "Ewa"
        self.run_import()
        self.assertEqual(Person.objects.filter(email__isnull=True).count(), 2)
        self.assertEqual(get_user_model().objects.filter(email="").count(), 2)

    def test_missing_and_nonempty_anthology_are_rejected(self):
        self.data["texts"][0]["anthology"] = "Nie istnieje"
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertEqual(Anthology.objects.count(), 1)
        self.data["texts"][0]["anthology"] = self.anthology.title
        Text.objects.create(title="Zajęte", length=1, anthology=self.anthology)
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertEqual(Text.objects.count(), 1)
        self.assertFalse(Person.objects.exists())

    def test_ambiguous_name_rolls_back_all_rows_and_reports_conflict(self):
        Person.objects.create(**self.person, email=None)
        Person.objects.create(**self.person, email=None)
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(
            Text.objects.exists() or Author.objects.exists() or get_user_model().objects.exists()
        )
        report = json.loads(self.report.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIn("Niejednoznaczna", report["conflicts"][0]["error"])

    def test_error_in_later_row_rolls_back_earlier_work_and_profiles(self):
        bad = copy.deepcopy(self.data["texts"][0])
        bad.update(source_row=2, title="Drugi tekst")
        bad["stages"][0]["person"]["person_id"] = 999999
        self.data["texts"].append(bad)
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(
            Text.objects.exists()
            or Person.objects.exists()
            or Author.objects.exists()
            or get_user_model().objects.exists()
        )

    def test_missing_measured_length_blocks_import(self):
        self.data["texts"][0]["length"] = None
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(Text.objects.exists())

    def test_explicit_profile_id_can_resolve_a_name_variant(self):
        person = Person.objects.create(first_name="Anna Maria", last_name="Testowa", email=None)
        for stage in self.data["texts"][0]["stages"]:
            stage["person"]["person_id"] = person.pk
        self.run_import()
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(get_user_model().objects.get().first_name, "Anna Maria")

    def test_verifiers_5_to_7_are_completed_history_visible_in_profile(self):
        from workflow.catalog import active_role_choices, active_stage_choices
        from core.selectors.people import imported_work_summary

        for kind in ("fifth_verification", "sixth_verification", "seventh_verification"):
            self.data["texts"][0]["stages"].append(
                {"stage_type": kind, "person": dict(self.person), "source_column": "Weryfikacja"}
            )
        self.run_import()
        summary = {r["role"]: r for r in imported_work_summary(Person.objects.get())}
        for number, kind in zip(
            (5, 6, 7), ("fifth_verification", "sixth_verification", "seventh_verification")
        ):
            role = f"verifier_{number}"
            self.assertEqual(summary[role]["executions"], 1)
            self.assertNotIn(role, dict(active_role_choices()))
            self.assertNotIn(kind, dict(active_stage_choices()))
            stage = S.objects.get(stage_type=kind)
            self.assertTrue(stage.is_completed and stage.imported_completed)
            self.assertFalse(stage.is_current or stage.is_released or stage.assignment.is_current)
            with self.assertRaises(IntegrityError), transaction.atomic():
                S.objects.filter(pk=stage.pk).update(is_current=True)
            with self.assertRaises(IntegrityError), transaction.atomic():
                A.objects.filter(pk=stage.assignment_id).update(is_current=True)
        self.assertFalse(self.report.with_suffix(".html").exists())

    def test_selected_anthology_ignores_previously_modified_other_anthology(self):
        self.run_import()
        old = Text.objects.get()
        old.length += 1
        old.save(update_fields=["length"])
        anthology = Anthology.objects.create(title="Zbrodnia doskonała", status="ready")
        extra = copy.deepcopy(self.data["texts"][0])
        extra.update(source_row=2, title="Nowy tekst", anthology=anthology.title)
        self.data["texts"].append(extra)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        call_command(
            "import_completed_table",
            str(self.path),
            apply=True,
            anthology=["Zbrodnia doskonała"],
            report=str(self.report),
            stdout=StringIO(),
        )
        self.assertEqual(Text.objects.count(), 2)
        old.refresh_from_db()
        self.assertEqual(old.length, 12346)
        self.assertFalse(self.report.with_suffix(".html").exists())

    def test_unknown_anthology_filter_blocks_write(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command(
                "import_completed_table",
                str(self.path),
                apply=True,
                anthology=["Literówka"],
                report=str(self.report),
                stdout=StringIO(),
            )
        self.assertFalse(Text.objects.exists())


class ArchiveFilesMixin:
    """29 tekstów archiwum przed i po normalizacji (wielokrotne wykonania, zmiana nazwiska osoby)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.folder = Path(tmp.name)
        Anthology.objects.create(title="Archiwum", status="ready")
        data = {
            "schema_version": 1,
            "source": SOURCE,
            "texts": [
                {
                    "source_row": i,
                    "title": f"Tekst {i}",
                    "anthology": "Archiwum",
                    "length": 100,
                    "length_source": {"sha256": "fixture"},
                    "authors": [{"first_name": "Anna", "last_name": "Autorka"}],
                    "stages": [
                        {
                            "stage_type": kind,
                            "source_column": "fixture",
                            "person": {"first_name": "Ilona", "last_name": "Skrzypczak"},
                        }
                        for kind in [
                            "editing",
                            "editing",
                            "first_proofreading",
                            "second_proofreading",
                            "first_verification",
                            "second_verification",
                            "seventh_verification",
                        ]
                    ],
                }
                for i in range(1, 30)
            ],
        }
        cls.before = cls.folder / "before.json"
        cls.after = cls.folder / "after.json"
        cls.before.write_text(json.dumps(data), encoding="utf-8")
        cls.after.write_text(json.dumps(normalized_archive(data)), encoding="utf-8")

    def repair(self, apply=True):
        call_command(
            "repair_archive_executions",
            before=str(self.before),
            after=str(self.after),
            apply=apply,
            report=str(self.folder / "repair"),
            stdout=StringIO(),
        )
        return json.loads((self.folder / "repair.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {
            m._meta.label: list(m.objects.order_by("pk").values())
            for m in [Text, S, A, Person, Author, get_user_model()]
        }


@tag('slow')
class ArchiveExecutionRepairTests(ArchiveFilesMixin, TestCase):
    """Naprawa istniejącego importu; dane startowe importowane raz dla całej klasy."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command(
            "import_completed_table",
            str(cls.before),
            apply=True,
            report=str(cls.folder / "old"),
            stdout=StringIO(),
        )

    def test_preview_apply_rerun_and_normal_import_preserve_ids_and_duplicate_people(self):
        before = self.snapshot()
        self.repair(False)
        self.assertEqual(before, self.snapshot())
        report = self.repair()
        self.assertEqual(report["counts"], {"korekta": 29})
        after = self.snapshot()
        for label, rows in before.items():
            self.assertEqual([r["id"] for r in rows], [r["id"] for r in after[label]])
        self.assertEqual(Person.objects.get().last_name, "Żurawska")
        self.assertEqual(get_user_model().objects.get().last_name, "Żurawska")
        for text in Text.objects.all():
            for kind, count in [
                ("editing", 2),
                ("first_proofreading", 2),
                ("first_verification", 3),
            ]:
                stages = list(
                    S.objects.filter(text=text, stage_type=kind).order_by("execution_number")
                )
                self.assertEqual([s.execution_number for s in stages], list(range(1, count + 1)))
                self.assertTrue(all(s.is_completed and s.imported_completed for s in stages))
                expected = {
                    "editing": "Redaktor",
                    "first_proofreading": "Korektor 1",
                    "first_verification": "Weryfikator 1",
                }[kind]
                for number, stage in enumerate(stages, 1):
                    label = expected if number == 1 else f"{expected} (wyk. {number})"
                    self.assertEqual(stage_label(stage), label)
                    self.assertEqual(assignment_label(stage.assignment), label)
                self.assertNotIn("przebieg", str(stages[0].assignment))
        self.assertEqual(S.objects.filter(stage_type="ready").count(), 29)
        report = self.repair()
        self.assertEqual(report["counts"], {"bez zmian": 29})
        self.assertEqual(after, self.snapshot())
        call_command(
            "import_completed_table",
            str(self.after),
            apply=True,
            report=str(self.folder / "new"),
            stdout=StringIO(),
        )
        self.assertEqual(after, self.snapshot())
        admin = get_user_model().objects.create_superuser(
            username="render-admin", email="admin@example.com", password="test"
        )
        self.client.force_login(admin)
        text = Text.objects.get(import_source_row=1)
        response = self.client.get(reverse("core:assigned_text_detail", args=[text.pk]))
        self.assertEqual(response.status_code, 200)
        team = response.context["team_members"]
        self.assertEqual(len(team), 7)
        self.assertEqual(
            [m["label"] for m in team if m["role"] == "verifier_1"],
            ["Weryfikator 1", "Weryfikator 1 (wyk. 2)", "Weryfikator 1 (wyk. 3)"],
        )
        self.assertContains(response, "Korektor 1 (wyk. 2)")
        self.assertContains(response, "Redaktor")

    def test_late_conflict_rolls_back_previous_changes_and_name(self):
        last = Text.objects.get(import_source_row=29)
        S.objects.filter(text=last, stage_type="editing", execution_number=1).update(
            ended_at="2020-01-01"
        )
        before = self.snapshot()
        with self.assertRaises(CommandError):
            self.repair()
        self.assertEqual(before, self.snapshot())

    def test_existing_zurawska_reused_without_deleting_old_profile(self):
        old = Person.objects.get()
        u = get_user_model().objects.create(
            username="existing", first_name="Ilona", last_name="Żurawska", email="keep@example.com"
        )
        new = Person.objects.create(
            first_name="Ilona", last_name="Żurawska", email="keep@example.com", user=u
        )
        self.repair()
        old.refresh_from_db()
        new.refresh_from_db()
        u.refresh_from_db()
        self.assertEqual(old.last_name, "Skrzypczak")
        self.assertEqual(new.email, "keep@example.com")
        self.assertEqual(u.email, "keep@example.com")
        self.assertEqual(set(A.objects.values_list("assigned_to_id", flat=True)), {u.pk})

    def test_ambiguous_ilona_rolls_back(self):
        Person.objects.create(first_name="Ilona", last_name="Skrzypczak", email=None)
        before = self.snapshot()
        with self.assertRaises(CommandError):
            self.repair()
        self.assertEqual(before, self.snapshot())


class ArchiveExecutionFreshImportTests(ArchiveFilesMixin, TestCase):
    @tag('slow')
    def test_no_existing_import_can_be_followed_by_fresh_import(self):
        result = self.repair()
        self.assertEqual(len(result["missing"]), 29)
        call_command(
            "import_completed_table",
            str(self.after),
            apply=True,
            report=str(self.folder / "new"),
            stdout=StringIO(),
        )
        self.assertEqual(Text.objects.count(), 29)
        self.assertFalse(S.objects.filter(stage_type="seventh_verification").exists())
        self.assertEqual(self.repair()["counts"], {"bez zmian": 29})

    def test_other_sources_keep_operational_labels(self):
        text = Text(title="Inny", length=1, import_source="other", import_source_row=1)
        s = S(
            text=text, stage_type="first_verification", execution_number=1, imported_completed=True
        )
        self.assertEqual(stage_label(s), "Pierwsza weryfikacja")
