from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, router
from django.utils import timezone

from texts.models import Text


class WorkflowStageQuerySet(models.QuerySet):
    def current_cycle(self):
        return self.filter(
            workflow_cycle=models.F("text__current_workflow_cycle"),
            is_current=True,
        )





class WorkflowStage(models.Model):
    class StageType(models.TextChoices):
        READY_FOR_EDITING = "ready_for_editing", "Do redakcji"
        EDITING = "editing", "Redakcja"
        FIRST_VERIFICATION = "first_verification", "Pierwsza weryfikacja"
        AUTHOR_EDITING = "author_editing", "Plik u autora (redakcja)"
        SECOND_VERIFICATION = "second_verification", "Druga weryfikacja"
        EDITING_CONTROL = "editing_control", "Kontrola K. redakcji"
        FIRST_PROOFREADING = "first_proofreading", "Pierwsza korekta"
        SECOND_PROOFREADING = "second_proofreading", "Druga korekta"
        THIRD_VERIFICATION = "third_verification", "Trzecia weryfikacja"
        COORDINATOR_CONTROL = (
            "coordinator_control",
            "Kontr. wer. (koordynator)",
        )
        EDITOR_CONTROL = "editor_control", "Kontr. wer. (redaktor)"
        THIRD_PROOFREADING = "third_proofreading", "Trzecia korekta"
        FOURTH_PROOFREADING = "fourth_proofreading", "Czwarta korekta"
        STYLING = "styling", "Stylowanie"
        READY = "ready", "Gotowe"
        WITHDRAWN = "withdrawn", "WYCOFANY"

    text = models.ForeignKey(
        Text,
        on_delete=models.CASCADE,
        related_name="workflow_stages",
        verbose_name="tekst",
    )

    workflow_cycle = models.PositiveIntegerField(
        "przebieg workflow",
        default=1,
        editable=False,
        validators=[MinValueValidator(1)],
    )

    execution_number = models.PositiveIntegerField("wykonanie etapu", default=1, editable=False)
    is_current = models.BooleanField("aktualne wykonanie", default=True, editable=False)
    is_released = models.BooleanField("dostępny w kolejce", default=True, editable=False)
    repetition = models.ForeignKey("WorkflowRepetition", null=True, blank=True, on_delete=models.RESTRICT, related_name="stages", editable=False)
    queue_position = models.PositiveIntegerField(default=0, editable=False)
    assignment = models.ForeignKey("WorkflowRoleAssignment", null=True, blank=True, on_delete=models.RESTRICT, related_name="stages", editable=False)

    stage_type = models.CharField(
        "etap",
        max_length=30,
        choices=StageType.choices,
    )

    iteration = models.PositiveSmallIntegerField(
        "numer podejścia",
        default=1,
        validators=[MinValueValidator(1)],
    )

    started_at = models.DateField(
        "data rozpoczęcia",
        null=True,
        blank=True,
    )

    ended_at = models.DateField(
        "data zakończenia",
        null=True,
        blank=True,
    )

    is_completed = models.BooleanField(
        "zakończone i gotowe do kolejnego etapu",
        default=False,
    )

    imported_completed = models.BooleanField(
        "zakończony etap z importu", default=False, editable=False,
        help_text="Wyłącznie import: zakończona praca może nie mieć znanych dat.",
    )

    # Domyślny manager zachowuje historię wszystkich przebiegów.
    objects = WorkflowStageQuerySet.as_manager()

    class Meta:
        verbose_name = "etap pracy"
        verbose_name_plural = "etapy pracy"
        ordering = (
            "text__title",
            "workflow_cycle",
            "started_at",
            "stage_type",
            "iteration",
            "pk",
        )
        indexes = [
            models.Index(
                fields=(
                    "text",
                    "workflow_cycle",
                    "is_completed",
                    "stage_type",
                ),
                name="wf_stage_cycle_state_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "text",
                    "workflow_cycle",
                    "stage_type",
                    "iteration",
                ),
                name="uniq_text_cycle_stage_iter",
            ),
            models.CheckConstraint(
                condition=models.Q(workflow_cycle__gte=1),
                name="wf_stage_cycle_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(iteration__gte=1),
                name="wf_stage_iteration_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(ended_at__isnull=True)
                    | (
                        (models.Q(started_at__isnull=False) & models.Q(ended_at__gte=models.F("started_at")))
                        | models.Q(imported_completed=True, started_at__isnull=True)
                    )
                ),
                name="wf_stage_dates_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_completed=False)
                    | models.Q(imported_completed=True)
                    | (
                        models.Q(started_at__isnull=False)
                        & models.Q(ended_at__isnull=False)
                    )
                ),
                name="wf_completed_has_dates",
            ),
            models.CheckConstraint(
                condition=models.Q(imported_completed=False) | models.Q(is_completed=True),
                name="wf_import_is_completed",
            ),
        ]

    def __str__(self):
        return (
            f"{self.text.title}: {self.get_stage_type_display()} – "
            f"przebieg {self.workflow_cycle}, "
            f"podejście {self.iteration}"
        )

    def _validate_import_origin(self):
        from workflow.import_context import importing_completed
        if importing_completed.get():
            return
        previous = type(self).objects.filter(pk=self.pk).values_list("imported_completed", flat=True).first() if self.pk and not self._state.adding else False
        if self.imported_completed != bool(previous):
            raise ValidationError("Wyjątek dla dat może nadać wyłącznie importer zakończonych etapów.")

    def save(self, *args, **kwargs):
        self._validate_import_origin()
        if not self.assignment_id and self.text_id:
            from workflow.services import STAGE_ROLES
            role = STAGE_ROLES.get(self.stage_type)
            if role:
                self.assignment = WorkflowRoleAssignment.objects.filter(text_id=self.text_id, workflow_cycle=self.workflow_cycle, role=role, is_current=True).first()
                if self.assignment_id and kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {"assignment"}
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()

        self._validate_import_origin()
        errors = {}
        if self.imported_completed and not self.is_completed:
            errors["is_completed"] = "Zaimportowany etap musi pozostać zakończony."

        if self.is_completed and self.started_at and self.started_at > timezone.localdate():
            errors["started_at"] = "Zakończona praca nie może mieć przyszłej daty rozpoczęcia."
        if self.ended_at:
            if self.ended_at > timezone.localdate():
                errors['ended_at'] = "Nie można zakończyć pracy z przyszłą datą."
        if self.ended_at and not self.started_at and not self.imported_completed:
            errors["started_at"] = (
                "Etap z datą zakończenia musi mieć datę rozpoczęcia."
            )

        if (
            self.started_at
            and self.ended_at
            and self.ended_at < self.started_at
        ):
            errors["ended_at"] = (
                "Data zakończenia nie może być wcześniejsza "
                "niż data rozpoczęcia."
            )

        if self.ended_at and not self.is_completed:
            errors["is_completed"] = "Etap z datą zakończenia musi być oznaczony jako zakończony."
        if self.text_id and self.is_current and self.is_released and not self.is_completed and self.ended_at is None:
            duplicate = type(self).objects.filter(text_id=self.text_id, workflow_cycle=self.workflow_cycle, stage_type=self.stage_type, is_current=True, is_released=True, is_completed=False, ended_at__isnull=True).exclude(pk=self.pk).exists()
            if duplicate:
                errors['stage_type'] = "W tym przebiegu istnieje już otwarty etap tego rodzaju."

        if self.is_completed and not self.started_at and not self.imported_completed:
            errors["started_at"] = (
                "Zakończony etap musi mieć datę rozpoczęcia."
            )

        if self.is_completed and not self.ended_at and not self.imported_completed:
            errors["ended_at"] = (
                "Zakończony etap musi mieć datę zakończenia."
            )

        if errors:
            raise ValidationError(errors)


class WorkflowRoleAssignmentQuerySet(models.QuerySet):
    def current_cycle(self):
        return self.filter(
            workflow_cycle=models.F("text__current_workflow_cycle"),
            is_current=True,
        )

    def assigned(self):
        return self.filter(assigned_to__isnull=False)

    def unassigned(self):
        return self.filter(assigned_to__isnull=True)


class WorkflowRoleAssignment(models.Model):
    class Role(models.TextChoices):
        EDITOR = "editor", "Redaktor"
        EDITING_COORDINATOR = "editing_coordinator", "K. redakcji"
        PROOFREADER_1 = "proofreader_1", "Korektor 1"
        PROOFREADER_2 = "proofreader_2", "Korektor 2"
        PROOFREADER_3 = "proofreader_3", "Korektor 3"
        PROOFREADER_4 = "proofreader_4", "Korektor 4"
        VERIFIER_1 = "verifier_1", "Weryfikator 1"
        VERIFIER_2 = "verifier_2", "Weryfikator 2"
        VERIFIER_3 = "verifier_3", "Weryfikator 3"
        VERIFICATION_COORDINATOR = (
            "verification_coordinator",
            "K. weryfikacji",
        )
        STYLING = "styling", "Stylowanie"

    text = models.ForeignKey(
        Text,
        on_delete=models.CASCADE,
        related_name="workflow_role_assignments",
        verbose_name="tekst",
    )

    workflow_cycle = models.PositiveIntegerField(
        "przebieg workflow",
        default=1,
        editable=False,
        validators=[MinValueValidator(1)],
    )

    execution_number = models.PositiveIntegerField("wykonanie przydziału", default=1, editable=False)
    is_current = models.BooleanField("aktualne przypisanie", default=True, editable=False)
    repetition = models.ForeignKey("WorkflowRepetition", null=True, blank=True, on_delete=models.RESTRICT, related_name="assignments", editable=False)

    role = models.CharField(
        "rola",
        max_length=30,
        choices=Role.choices,
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="workflow_role_assignments",
        verbose_name="przypisana osoba",
        null=True,
        blank=True,
    )

    assigned_at = models.DateTimeField(
        "data przypisania",
        null=True,
        blank=True,
        editable=False,
        db_index=True,
    )

    notes = models.TextField(
        "uwagi",
        blank=True,
    )

    # Historyczne przydziały pozostają dostępne po zmianie przebiegu.
    objects = WorkflowRoleAssignmentQuerySet.as_manager()

    class Meta:
        verbose_name = "przypisanie roli"
        verbose_name_plural = "przypisania ról"
        ordering = (
            "text__title",
            "workflow_cycle",
            "role",
            "pk",
        )
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "text",
                    "workflow_cycle",
                    "role",
                    "execution_number",
                ),
                name="uniq_text_role_execution",
            ),
            models.UniqueConstraint(
                models.F("text"), models.F("workflow_cycle"), models.F("role"),
                models.Case(models.When(is_current=True, then=models.Value(1)), default=models.Value(None), output_field=models.IntegerField()),
                name="uniq_current_role_execution",
            ),
            # MySQL nie obsługuje indeksów UNIQUE z warunkiem WHERE.
            # NULL wyłącza inne role z ograniczenia; obie weryfikacje mają 1.
            # Wyrażenie zależy tylko od role, nie od kolumn kluczy obcych.
            models.UniqueConstraint(
                models.F("text"),
                models.F("workflow_cycle"),
                models.F("assigned_to"),
                models.Case(
                    models.When(
                        is_current=True, role__in=("verifier_1", "verifier_2"),
                        then=models.Value(1),
                    ),
                    default=models.Value(None),
                    output_field=models.IntegerField(),
                ),
                name="uniq_v1_v2_user_cycle",
            ),
            models.CheckConstraint(
                condition=models.Q(workflow_cycle__gte=1),
                name="wf_assignment_cycle_positive",
            ),
        ]

    def __str__(self):
        if self.assigned_to_id is not None:
            assigned_person = (
                self.assigned_to.get_full_name()
                or self.assigned_to.get_username()
            )
        else:
            assigned_person = "nieprzypisane"

        return (
            f"{self.text.title} – {self.get_role_display()} – "
            f"{assigned_person} – przebieg {self.workflow_cycle}"
        )

    def clean(self):
        super().clean()
        if self.role == self.Role.STYLING and self.assigned_to_id and not self.assigned_to.is_superuser:
            raise ValidationError({'assigned_to': 'Stylowanie można przypisać tylko superuserowi.'})


        protected_verifier_roles = (
            self.Role.VERIFIER_1,
            self.Role.VERIFIER_2,
        )

        if (
            not self.text_id
            or not self.assigned_to_id
            or not self.workflow_cycle
            or not self.is_current
            or self.role not in protected_verifier_roles
        ):
            return

        using = self._state.db or router.db_for_write(
            type(self),
            instance=self,
        )

        conflicts = (
            type(self).objects.using(using)
            .filter(
                text_id=self.text_id,
                workflow_cycle=self.workflow_cycle,
                assigned_to_id=self.assigned_to_id,
                role__in=protected_verifier_roles,
                is_current=True,
            )
            .exclude(pk=self.pk)
        )

        if conflicts.exists():
            raise ValidationError(
                {
                    "assigned_to": (
                        "Ta osoba jest już przypisana do pierwszej albo "
                        "drugiej weryfikacji tego tekstu. Pierwszą i drugą "
                        "weryfikację muszą wykonywać różne osoby."
                    ),
                }
            )

    def save(
        self,
        *,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        using = using or router.db_for_write(
            type(self),
            instance=self,
        )

        if update_fields is not None:
            update_fields = set(update_fields)

            if not update_fields:
                return

        assignment_is_saved = (
            update_fields is None
            or bool(
                {"assigned_to", "assigned_to_id"}.intersection(
                    update_fields
                )
            )
        )

        if assignment_is_saved:
            previous = None

            if self.pk is not None and not self._state.adding:
                previous = (
                    type(self).objects.using(using)
                    .filter(pk=self.pk)
                    .values("assigned_to_id")
                    .first()
                )

            assignment_changed = (
                previous is None
                or previous["assigned_to_id"] != self.assigned_to_id
            )

            if self.assigned_to_id is not None:
                if assignment_changed:
                    self.assigned_at = timezone.now()

                    if update_fields is not None:
                        update_fields.add("assigned_at")
            elif assignment_changed:
                # Jawne zwolnienie roli czyści datę.
                # Usunięcie konta przez SET_NULL omija save()
                # i zachowuje historyczną datę przypisania.
                self.assigned_at = None

                if update_fields is not None:
                    update_fields.add("assigned_at")

        # Kontrola uprawnień, dostępności osoby i dozwolonych przejść
        # należy do serwisów. Zmiany workflow muszą blokować rekord Text
        # w transakcji, a następnie zapisywać etap i przydziały.
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        if self.is_current:
            from workflow.services import STAGE_ROLES
            kinds = [kind for kind, role in STAGE_ROLES.items() if role == self.role]
            WorkflowStage.objects.using(using).filter(text_id=self.text_id, workflow_cycle=self.workflow_cycle, is_current=True, is_completed=False, assignment__isnull=True, stage_type__in=kinds).update(assignment_id=self.pk)

class WorkflowRepetition(models.Model):
    text = models.ForeignKey(Text, on_delete=models.CASCADE, related_name="repetitions")
    selected_stages = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    canceled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="canceled_workflow_repetitions")
    cancellation_reason = models.CharField(max_length=255, blank=True)
    previous_stage_ids = models.JSONField(default=list, editable=False)
    previous_assignment_ids = models.JSONField(default=list, editable=False)

    class Meta:
        verbose_name = "powtórzenie etapów"
        verbose_name_plural = "powtórzenia etapów"

    def __str__(self):
        return f"{self.text} — powtórzenie #{self.pk}"


class WorkflowHandoff(models.Model):
    text = models.ForeignKey(Text, on_delete=models.CASCADE, related_name="workflow_handoffs")
    stage = models.ForeignKey(WorkflowStage, on_delete=models.PROTECT, related_name="handoffs")
    previous_assignment = models.ForeignKey(WorkflowRoleAssignment, on_delete=models.PROTECT, related_name="handoffs_from")
    new_assignment = models.ForeignKey(WorkflowRoleAssignment, on_delete=models.PROTECT, related_name="handoffs_to")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    original_started_at = models.DateField(null=True, blank=True)
    reason = models.TextField()

    class Meta:
        verbose_name = "przekazanie pracy"
        verbose_name_plural = "przekazania pracy"
