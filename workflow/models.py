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
        )

    def pending(self):
        return self.filter(
            is_completed=False,
            started_at__isnull=True,
        )

    def active(self):
        return self.filter(
            is_completed=False,
            started_at__isnull=False,
            ended_at__isnull=True,
        )

    def completed(self):
        return self.filter(is_completed=True)


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
                        models.Q(started_at__isnull=False)
                        & models.Q(ended_at__gte=models.F("started_at"))
                    )
                ),
                name="wf_stage_dates_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_completed=False)
                    | (
                        models.Q(started_at__isnull=False)
                        & models.Q(ended_at__isnull=False)
                    )
                ),
                name="wf_completed_has_dates",
            ),
        ]

    def __str__(self):
        return (
            f"{self.text.title}: {self.get_stage_type_display()} – "
            f"przebieg {self.workflow_cycle}, "
            f"podejście {self.iteration}"
        )

    def clean(self):
        super().clean()

        errors = {}

        if self.ended_at and not self.started_at:
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

        if self.is_completed and not self.started_at:
            errors["started_at"] = (
                "Zakończony etap musi mieć datę rozpoczęcia."
            )

        if self.is_completed and not self.ended_at:
            errors["ended_at"] = (
                "Zakończony etap musi mieć datę zakończenia."
            )

        if errors:
            raise ValidationError(errors)


class WorkflowRoleAssignmentQuerySet(models.QuerySet):
    def current_cycle(self):
        return self.filter(
            workflow_cycle=models.F("text__current_workflow_cycle"),
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
                ),
                name="uniq_text_cycle_role",
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
                        role__in=("verifier_1", "verifier_2"),
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