import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("texts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkflowStage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "workflow_cycle",
                    models.PositiveIntegerField(
                        default=1,
                        editable=False,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                        ],
                        verbose_name="przebieg workflow",
                    ),
                ),
                (
                    "stage_type",
                    models.CharField(
                        choices=[
                            ("ready_for_editing", "Do redakcji"),
                            ("editing", "Redakcja"),
                            ("first_verification", "Pierwsza weryfikacja"),
                            ("author_editing", "Plik u autora (redakcja)"),
                            ("second_verification", "Druga weryfikacja"),
                            ("editing_control", "Kontrola K. redakcji"),
                            ("first_proofreading", "Pierwsza korekta"),
                            ("second_proofreading", "Druga korekta"),
                            ("third_verification", "Trzecia weryfikacja"),
                            (
                                "coordinator_control",
                                "Kontr. wer. (koordynator)",
                            ),
                            ("editor_control", "Kontr. wer. (redaktor)"),
                            ("third_proofreading", "Trzecia korekta"),
                            ("fourth_proofreading", "Czwarta korekta"),
                            ("styling", "Stylowanie"),
                            ("ready", "Gotowe"),
                            ("withdrawn", "WYCOFANY"),
                        ],
                        max_length=30,
                        verbose_name="etap",
                    ),
                ),
                (
                    "iteration",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                        ],
                        verbose_name="numer podejścia",
                    ),
                ),
                (
                    "started_at",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="data rozpoczęcia",
                    ),
                ),
                (
                    "ended_at",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="data zakończenia",
                    ),
                ),
                (
                    "is_completed",
                    models.BooleanField(
                        default=False,
                        verbose_name="zakończone i gotowe do kolejnego etapu",
                    ),
                ),
                (
                    "text",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflow_stages",
                        to="texts.text",
                        verbose_name="tekst",
                    ),
                ),
            ],
            options={
                "verbose_name": "etap pracy",
                "verbose_name_plural": "etapy pracy",
                "ordering": (
                    "text__title",
                    "workflow_cycle",
                    "started_at",
                    "stage_type",
                    "iteration",
                    "pk",
                ),
                "indexes": [
                    models.Index(
                        fields=[
                            "text",
                            "workflow_cycle",
                            "is_completed",
                            "stage_type",
                        ],
                        name="wf_stage_cycle_state_idx",
                    ),
                ],
                "constraints": [
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
                                & models.Q(
                                    ended_at__gte=models.F("started_at")
                                )
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
                ],
            },
        ),
        migrations.CreateModel(
            name="WorkflowRoleAssignment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "workflow_cycle",
                    models.PositiveIntegerField(
                        default=1,
                        editable=False,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                        ],
                        verbose_name="przebieg workflow",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("editor", "Redaktor"),
                            ("editing_coordinator", "K. redakcji"),
                            ("proofreader_1", "Korektor 1"),
                            ("proofreader_2", "Korektor 2"),
                            ("proofreader_3", "Korektor 3"),
                            ("proofreader_4", "Korektor 4"),
                            ("verifier_1", "Weryfikator 1"),
                            ("verifier_2", "Weryfikator 2"),
                            ("verifier_3", "Weryfikator 3"),
                            (
                                "verification_coordinator",
                                "K. weryfikacji",
                            ),
                            ("styling", "Stylowanie"),
                        ],
                        max_length=30,
                        verbose_name="rola",
                    ),
                ),
                (
                    "assigned_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        editable=False,
                        null=True,
                        verbose_name="data przypisania",
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        verbose_name="uwagi",
                    ),
                ),
                (
                    "text",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflow_role_assignments",
                        to="texts.text",
                        verbose_name="tekst",
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="workflow_role_assignments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="przypisana osoba",
                    ),
                ),
            ],
            options={
                "verbose_name": "przypisanie roli",
                "verbose_name_plural": "przypisania ról",
                "ordering": (
                    "text__title",
                    "workflow_cycle",
                    "role",
                    "pk",
                ),
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "text",
                            "workflow_cycle",
                            "role",
                        ),
                        name="uniq_text_cycle_role",
                    ),
                    models.UniqueConstraint(
                        fields=(
                            "text",
                            "workflow_cycle",
                            "assigned_to",
                        ),
                        condition=(
                            models.Q(
                                role__in=("verifier_1", "verifier_2"),
                            )
                            & models.Q(assigned_to__isnull=False)
                        ),
                        name="uniq_v1_v2_user_cycle",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(workflow_cycle__gte=1),
                        name="wf_assignment_cycle_positive",
                    ),
                ],
            },
        ),
    ]