"""Praca zaimportowana z archiwum: etapy tylko z importu, profil osoby, listy admina i jednorazowa naprawa importu „Ballad”."""
from datetime import date, timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.management.commands.repair_ballady_import import repair, SOURCE, BOOK, TARGETS
from people.models import Person, Role
from texts.models import Text, Anthology
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES, IMPORT_ONLY_ROLES, active_stage_choices, active_role_choices
from workflow.completed_import import import_completed_workflow
from workflow.read_queries import annotate_my_work, dashboard_querysets
from workflow.repetitions import repeat_stages
from workflow.services import _ensure_editing_phase, claim_stage, create_pending_stage, resume_editing, user_can_complete_stage

from .support import importing


class ImportedWorkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            username="admin@example.com", email="admin@example.com", password="test-password")
        cls.people = []
        for number in range(3):
            user = get_user_model().objects.create_user(
                username=f"member{number}@example.com", email=f"member{number}@example.com")
            person = Person.objects.create(first_name="Osoba", last_name=str(number), user=user, email=user.email)
            role, _ = Role.objects.get_or_create(name="Redaktor")
            person.roles.add(role)
            cls.people.append(person)
        cls.anthology = Anthology.objects.create(title="Import")

    def setUp(self):
        self.text = Text.objects.create(title="Zakończona praca", length=100, anthology=self.anthology)

    def row(self, kind, person=0, **kwargs):
        return {"stage_type": kind, "assigned_to": self.people[person].user.email, **kwargs}

    def load(self, rows=None, next_stage="ready"):
        return import_completed_workflow(text_id=self.text.pk, next_stage=next_stage, stages=rows or [
            self.row("editing", 0), self.row("editing", 1),
            self.row("editing_review", 2), self.row("fourth_verification", 2),
        ])

    def test_import_retains_people_and_unknown_dates_and_is_repeatable(self):
        self.assertTrue(self.load())
        self.assertFalse(self.load())
        self.assertEqual(S.objects.filter(text=self.text).count(), 5)
        self.assertEqual(list(S.objects.filter(stage_type="editing").order_by("pk").values_list(
            "execution_number", "assignment__assigned_to_id")), [(1, self.people[0].user_id), (2, self.people[1].user_id)])
        self.assertFalse(S.objects.filter(imported_completed=True).filter(started_at__isnull=False).exists())
        self.assertFalse(S.objects.filter(imported_completed=True).filter(ended_at__isnull=False).exists())
        self.assertFalse(A.objects.filter(assigned_at__isnull=False).exists())
        self.assertFalse(S.objects.current_cycle().filter(stage_type__in=IMPORT_ONLY_STAGE_TYPES).exists())
        self.assertFalse(A.objects.current_cycle().filter(role__in=IMPORT_ONLY_ROLES).exists())
        self.assertEqual(A.objects.filter(role="editor", is_current=True).get().assigned_to_id, self.people[1].user_id)

    def test_same_old_verifier_is_preserved_without_live_v1_v2_conflict(self):
        rows = [self.row("first_verification"), self.row("second_verification"), self.row("fourth_verification")]
        self.assertTrue(self.load(rows))
        self.assertFalse(self.load(rows))
        self.assertEqual(S.objects.filter(imported_completed=True, assignment__assigned_to=self.people[0].user).count(), 3)
        self.assertEqual(A.objects.current_cycle().filter(role__in=("verifier_1", "verifier_2")).count(), 1)

    def test_import_only_types_cannot_be_created_or_activated_by_normal_workflow(self):
        for kind in IMPORT_ONLY_STAGE_TYPES:
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                create_pending_stage(self.text, kind)
        for role in IMPORT_ONLY_ROLES:
            with self.subTest(role=role), self.assertRaises(ValidationError):
                A.objects.create(text=self.text, role=role, assigned_to=self.people[0].user, is_current=False)
        self.load()
        for stage in S.objects.filter(stage_type__in=IMPORT_ONLY_STAGE_TYPES):
            self.assertFalse(user_can_complete_stage(stage, self.admin))
            with self.assertRaises(ValidationError):
                repeat_stages(self.text, [stage.stage_type], self.admin)
            with self.assertRaises(ValidationError):
                claim_stage(self.text, stage.stage_type, self.admin)
            with self.assertRaises(IntegrityError), transaction.atomic():
                S.objects.filter(pk=stage.pk).update(is_current=True)
            with self.assertRaises(IntegrityError), transaction.atomic():
                A.objects.filter(pk=stage.assignment_id).update(is_current=True)
            stage.assignment.assigned_to = self.people[0].user
            with self.assertRaises(ValidationError):
                stage.assignment.save()

    def test_imported_completed_editor_counts_but_later_repeat_does_not(self):
        self.load()
        self.assertFalse(S.objects.filter(stage_type="editing_control").exists())
        for person in self.people[:2]:
            work = annotate_my_work(Text.objects.all(), person.user, timezone.localdate()).get(pk=self.text.pk)
            self.assertTrue(work.work_completed)
            self.assertFalse(work.work_active or work.work_waiting)
        run = repeat_stages(self.text, ["editing"], self.admin)
        self.assertEqual(list(run.stages.values_list("stage_type", flat=True)), ["editing"])
        for person in self.people[:2]:
            self.assertFalse(annotate_my_work(Text.objects.all(), person.user, timezone.localdate()).get(pk=self.text.pk).work_completed)
        self.assertEqual(S.objects.filter(stage_type__in=IMPORT_ONLY_STAGE_TYPES).count(), 2)

    def test_import_only_work_is_in_profile_summary_but_not_assignments_or_work_lists(self):
        from core.views.people import _profile_assignments, _imported_work_summary
        from core.selectors.texts import my_texts_context
        self.load()
        person = self.people[2]
        summary = _imported_work_summary(person)
        self.assertEqual({r["role"]: r["texts"] for r in summary}, {"editing_reviewer": 1, "verifier_4": 1})
        rows, totals = _profile_assignments(person, include_authors=False)
        self.assertEqual(rows, [])
        self.assertEqual(totals, {"active": 0, "reserved": 0, "completed": 0})
        self.assertFalse(list(my_texts_context(user=person.user, selected_view="all", params=QueryDict())["texts"]))
        for query in dashboard_querysets(person.user, timezone.localdate()):
            self.assertFalse(query.exists())
        self.client.force_login(person.user)
        response = self.client.get(reverse("core:person_detail", args=[person.pk]))
        self.assertContains(response, "Dawna aktywność")
        self.assertContains(response, "Weryfikator 4")
        self.assertContains(response, "Kontrola redakcji")
        self.assertEqual(response.context["assignments"], [])

    def test_filters_tables_and_people_tiles_hide_import_only_work_and_styling_tile(self):
        from core.selectors.texts import text_detail_context, workflow_list_context
        self.load()
        styling = A.objects.create(text=self.text, role="styling", assigned_to=self.admin)
        context = text_detail_context(user=self.admin, text=self.text)
        self.assertFalse({m["role"] for m in context["team_members"]} & {*IMPORT_ONLY_ROLES, "styling"})
        self.assertTrue(A.objects.filter(pk=styling.pk).exists())
        self.assertTrue({"editing_review", "fourth_verification"}.issubset({s["stage_type"] for s in context["archived_stages"]}))
        self.assertFalse(set(dict(active_role_choices())) & set(IMPORT_ONLY_ROLES))
        self.assertFalse(set(dict(active_stage_choices())) & set(IMPORT_ONLY_STAGE_TYPES))
        listing = workflow_list_context(user=self.admin, params=QueryDict())
        self.assertFalse({role for role, _ in listing["role_columns"]} & set(IMPORT_ONLY_ROLES))
        self.assertFalse({row["stage_type"] for row in listing["stages"]} & set(IMPORT_ONLY_STAGE_TYPES))

    def test_admin_lists_hide_import_only_types_but_record_details_keep_date_correction(self):
        self.load()
        self.client.force_login(self.admin)
        for model, key, excluded in ((S, "stage_type", IMPORT_ONLY_STAGE_TYPES), (A, "role", IMPORT_ONLY_ROLES)):
            response = self.client.get(reverse(f"admin:workflow_{model._meta.model_name}_changelist"))
            self.assertEqual(response.status_code, 200)
            self.assertFalse({getattr(r, key) for r in response.context["cl"].result_list} & set(excluded))
            self.assertEqual(response.context["cl"].full_result_count, model.objects.exclude(**{key + "__in": excluded}).count())
            choices = next(f.lookup_choices for f in response.context["cl"].filter_specs if getattr(f, "parameter_name", None) == key)
            self.assertFalse(set(dict(choices)) & set(excluded))
        stage = S.objects.get(stage_type="fourth_verification")
        response = self.client.get(reverse("admin:workflow_workflowstage_change", args=[stage.pk]))
        self.assertEqual(response.status_code, 200)
        stage.started_at = timezone.localdate() - timedelta(days=2)
        stage.ended_at = timezone.localdate() - timedelta(days=1)
        stage.full_clean()
        stage.save()
        self.assertFalse(stage.is_current)
        response = self.client.get(reverse("admin:texts_text_change", args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        for inline in response.context["inline_admin_formsets"]:
            if inline.opts.model is S:
                self.assertFalse(
                    inline.formset.get_queryset()
                    .filter(stage_type__in=IMPORT_ONLY_STAGE_TYPES)
                    .exists()
                )
            if inline.opts.model is A:
                self.assertFalse(
                    inline.formset.get_queryset().filter(role__in=IMPORT_ONLY_ROLES).exists()
                )
        response = self.client.get(reverse("core:assigned_text_detail", args=[self.text.pk]))
        self.assertContains(
            response, reverse("admin:workflow_workflowstage_change", args=[stage.pk])
        )

    def test_shared_import_command_previews_then_creates_missing_people_without_roles(self):
        data = {
            "schema_version": 1,
            "source": "test-import-only",
            "people": [
                {"first_name": "Dawny", "last_name": "Wykonawca", "email": "past@example.com"}
            ],
            "authors": [
                {"first_name": "Autor", "last_name": "Testowy", "email": "writer@example.com"}
            ],
            "texts": [
                {
                    "source_row": 1,
                    "title": "Dawne zgłoszenie",
                    "anthology": "Dawna antologia",
                    "length": 100,
                    "authors": ["writer@example.com"],
                    "next_stage": "ready",
                    "stages": [
                        {"stage_type": kind, "assigned_to": "past@example.com"}
                        for kind in IMPORT_ONLY_STAGE_TYPES
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            call_command("import_team_archive", str(path), stdout=StringIO())
            self.assertFalse(Person.objects.filter(email="past@example.com").exists())
            self.assertFalse(Text.objects.filter(import_source=data["source"]).exists())
            for _ in range(2):
                call_command("import_team_archive", str(path), commit=True, stdout=StringIO())
        person = Person.objects.select_related("user").get(email="past@example.com")
        self.assertFalse(
            person.is_active
            or person.user.is_active
            or person.user.is_staff
            or person.user.is_superuser
        )
        self.assertFalse(person.user.has_usable_password())
        self.assertFalse(person.roles.exists())
        self.assertEqual(
            S.objects.filter(text__import_source=data["source"], imported_completed=True).count(),
            len(IMPORT_ONLY_STAGE_TYPES),
        )

    def test_invalid_import_rolls_back_every_assignment(self):
        with self.assertRaises(ValidationError):
            self.load([self.row("editing"), self.row("fourth_verification", ended_at="not-a-date")])
        self.assertFalse(A.objects.filter(text=self.text).exists())
        self.assertFalse(S.objects.filter(text=self.text).exists())


class RepairBalladyImportTests(TestCase):
    """Jednorazowa naprawa importu antologii „Ballady”: daty pracy z autorem i wznowienie redakcji."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser('editor', 'editor@example.com', 'password')
        self.book = Anthology.objects.create(title=BOOK)
        with importing():
            for row, title, end in TARGETS:
                t = Text.objects.create(title=title, length=123, anthology=self.book,
                                        import_source=SOURCE, import_source_row=row)
                for n in range(1, 4 if row == 1 else 2):
                    a = A.objects.create(text=t, role='editor', assigned_to=self.user, execution_number=n,
                                         is_current=n == (3 if row == 1 else 1))
                    S.objects.create(text=t, stage_type='editing', assignment=a, iteration=n, execution_number=n,
                                     is_current=a.is_current, is_completed=True, imported_completed=True)
                editor = a
                for role, kind in [('editing_coordinator', 'editing_control'), ('proofreader_1', 'first_proofreading'),
                                   ('verifier_1', 'first_verification')]:
                    a = A.objects.create(text=t, role=role, assigned_to=self.user)
                    dated = end if kind == 'first_verification' else None
                    S.objects.create(text=t, stage_type=kind, assignment=a, is_completed=True,
                                     imported_completed=True, started_at=dated, ended_at=dated)
                S.objects.create(text=t, stage_type='author_editing', assignment=editor)

    def test_preview_repair_repeat_and_resume(self):
        counts = (Text.objects.count(), A.objects.count(), S.objects.count())
        repair()
        self.assertFalse(
            S.objects.filter(stage_type="author_editing", started_at__isnull=False).exists()
        )
        repair(apply=True)
        repair(apply=True)
        self.assertEqual(counts, (Text.objects.count(), A.objects.count(), S.objects.count()))
        for _, title, end in TARGETS:
            text = Text.objects.get(title=title)
            self.assertEqual(S.objects.get(text=text, stage_type="author_editing").started_at, end)
            _ensure_editing_phase(text)
            stage = resume_editing(text, self.user, started_at=date(2026, 9, 29))
            self.assertEqual(stage.stage_type, "editing")
            self.assertFalse(stage.is_completed)
        repair(apply=True)  # No resetting of subsequent legitimate work.

    def test_changed_second_text_blocks_all_writes(self):
        S.objects.filter(text__title=TARGETS[1][1], stage_type="author_editing").update(
            started_at=date(2026, 9, 25)
        )
        with self.assertRaises(CommandError):
            repair(apply=True)
        self.assertIsNone(
            S.objects.get(text__title=TARGETS[0][1], stage_type="author_editing").started_at
        )
        self.assertTrue(
            S.objects.get(text__title=TARGETS[0][1], stage_type="first_proofreading").is_current
        )
