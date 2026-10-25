"""Zakończona praca z dawnych importów (fabryka import_completed_workflow): etapy tylko z importu, profil osoby,
listy admina oraz etykiety wykonań tekstów archiwalnych."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from people.models import Person, Role
from texts.models import Text, Anthology
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES, IMPORT_ONLY_ROLES, active_stage_choices, active_role_choices
from fantazmaty.testing.history import import_completed_workflow
from workflow.read_queries import dashboard_querysets
from workflow.repetitions import repeat_stages
from workflow.archive_executions import SOURCE as ARCHIVE_SOURCE
from workflow.labels import assignment_label, stage_label
from workflow.services import claim_stage, create_pending_stage, user_can_complete_stage

from .support import work_state


class ImportedWorkTests(TestCase):
    """Tekst z zakończoną historią: dwie redakcje, kontrola redakcji i czwarta weryfikacja z dawnego importu."""

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
            work = work_state(person.user, self.text)
            self.assertTrue(work.work_completed)
            self.assertFalse(work.work_active or work.work_waiting)
        run = repeat_stages(self.text, ["editing"], self.admin)
        self.assertEqual(list(run.stages.values_list("stage_type", flat=True)), ["editing"])
        for person in self.people[:2]:
            self.assertFalse(work_state(person.user, self.text).work_completed)
        self.assertEqual(S.objects.filter(stage_type__in=IMPORT_ONLY_STAGE_TYPES).count(), 2)

    def test_import_only_work_is_in_profile_summary_but_not_assignments_or_work_lists(self):
        from core.views.people import _profile_assignments, _imported_work_summary
        from core.selectors.texts import my_texts_context
        self.load([self.row("editing", 0), *[self.row(kind, 2) for kind in IMPORT_ONLY_STAGE_TYPES]])
        person = self.people[2]
        summary = _imported_work_summary(person)
        self.assertEqual({r["role"]: r["texts"] for r in summary}, dict.fromkeys(IMPORT_ONLY_ROLES, 1))
        rows, totals = _profile_assignments(person, include_authors=False)
        self.assertEqual(rows, [])
        self.assertEqual(totals, {"active": 0, "reserved": 0, "completed": 0})
        self.assertFalse(list(my_texts_context(user=person.user, selected_view="all", params=QueryDict())["texts"]))
        for query in dashboard_querysets(person.user, timezone.localdate()):
            self.assertFalse(query.exists())
        self.client.force_login(person.user)
        response = self.client.get(reverse("core:person_detail", args=[person.pk]))
        imported = response.context["imported_work_summary"]
        self.assertEqual({r["role"]: r["texts"] for r in imported}, dict.fromkeys(IMPORT_ONLY_ROLES, 1))
        self.assertLessEqual({"Weryfikator 4", "Kontrola redakcji"}, {r["label"] for r in imported})
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

    def test_invalid_import_rolls_back_every_assignment(self):
        with self.assertRaises(ValidationError):
            self.load([self.row("editing"), self.row("fourth_verification", ended_at="not-a-date")])
        self.assertFalse(A.objects.filter(text=self.text).exists())
        self.assertFalse(S.objects.filter(text=self.text).exists())

    def test_archive_table_texts_use_role_names_with_execution_numbers(self):
        self.load()
        stage = S.objects.get(stage_type="editing", execution_number=2)
        self.assertEqual(stage_label(stage), "Redakcja (wyk. 2)")
        Text.objects.filter(pk=self.text.pk).update(import_source=ARCHIVE_SOURCE, import_source_row=1)
        stage = S.objects.select_related("text", "assignment__text").get(pk=stage.pk)
        self.assertEqual(stage_label(stage), "Redaktor (wyk. 2)")
        self.assertEqual(str(stage), "Zakończona praca: Redaktor (wyk. 2)")
        self.assertEqual(assignment_label(stage.assignment), "Redaktor (wyk. 2)")
        self.assertEqual(str(stage.assignment), "Zakończona praca – Redaktor (wyk. 2) – Osoba 1")
        first = S.objects.get(stage_type="editing", execution_number=1)
        self.assertEqual(stage_label(first), "Redaktor")
        # Rows outside the 29 archive rows keep the ordinary stage labels.
        Text.objects.filter(pk=self.text.pk).update(import_source_row=30)
        self.assertEqual(stage_label(S.objects.get(pk=stage.pk)), "Redakcja (wyk. 2)")
