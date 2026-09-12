import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("authors", "0001_initial"),
        ("people", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Anthology",
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
                    "title",
                    models.CharField(
                        max_length=255,
                        verbose_name="tytuł antologii",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("published", "Wydane"),
                            ("unpublished", "Niewydane"),
                            ("in_preparation", "W przygotowaniu"),
                        ],
                        default="unpublished",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                (
                    "has_illustrations",
                    models.BooleanField(
                        default=False,
                        verbose_name="ilustracje",
                    ),
                ),
                (
                    "print_status",
                    models.CharField(
                        choices=[
                            ("no", "Nie"),
                            ("planned", "Planowane"),
                            ("yes", "Tak"),
                        ],
                        default="no",
                        max_length=20,
                        verbose_name="wydanie drukowane",
                    ),
                ),
                (
                    "cover_status",
                    models.CharField(
                        choices=[
                            ("not_started", "Nierozpoczęta"),
                            ("in_progress", "W przygotowaniu"),
                            ("ready", "Gotowa"),
                        ],
                        default="not_started",
                        max_length=20,
                        verbose_name="stan okładki",
                    ),
                ),
                (
                    "cover_author",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        verbose_name="autor okładki",
                    ),
                ),
                (
                    "cover_notes",
                    models.TextField(
                        blank=True,
                        verbose_name="informacje o okładce",
                    ),
                ),
            ],
            options={
                "verbose_name": "antologia",
                "verbose_name_plural": "antologie",
                "ordering": ("title", "pk"),
            },
        ),
        migrations.CreateModel(
            name="AnthologyTask",
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
                    "task_type",
                    models.CharField(
                        choices=[
                            ("typesetting", "Skład"),
                            ("blurb", "Blurb"),
                            ("banners", "Bannery"),
                        ],
                        max_length=20,
                        verbose_name="rodzaj zadania",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("not_commissioned", "Niezlecone"),
                            ("commissioned", "Zlecone"),
                            ("ready", "Gotowe"),
                        ],
                        db_index=True,
                        default="not_commissioned",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                (
                    "commissioned_at",
                    models.DateField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name="data zlecenia",
                    ),
                ),
                (
                    "anthology",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="production_tasks",
                        to="texts.anthology",
                        verbose_name="antologia",
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="anthology_tasks",
                        to="people.person",
                        verbose_name="przypisana osoba",
                    ),
                ),
            ],
            options={
                "verbose_name": "zadanie antologii",
                "verbose_name_plural": "zadania antologii",
                "ordering": (
                    "anthology__title",
                    "task_type",
                    "pk",
                ),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("anthology", "task_type"),
                        name="unique_task_type_per_anthology",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Text",
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
                    "title",
                    models.CharField(
                        max_length=255,
                        verbose_name="tytuł",
                    ),
                ),
                (
                    "length",
                    models.PositiveIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                        ],
                        verbose_name="długość",
                    ),
                ),
                (
                    "content_warnings",
                    models.TextField(
                        blank=True,
                        verbose_name="trigger warningi",
                    ),
                ),
                (
                    "coordinator_note",
                    models.TextField(
                        blank=True,
                        verbose_name="notatka koordynatora",
                    ),
                ),
                (
                    "current_workflow_cycle",
                    models.PositiveIntegerField(
                        default=1,
                        editable=False,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                        ],
                        verbose_name="aktualny przebieg workflow",
                    ),
                ),
                (
                    "authors",
                    models.ManyToManyField(
                        related_name="texts",
                        to="authors.author",
                        verbose_name="autorzy",
                    ),
                ),
                (
                    "anthology",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="texts",
                        to="texts.anthology",
                        verbose_name="antologia",
                    ),
                ),
            ],
            options={
                "verbose_name": "tekst",
                "verbose_name_plural": "teksty",
                "ordering": ("title", "pk"),
            },
        ),
        migrations.CreateModel(
            name="TextNote",
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
                    "content",
                    models.TextField(
                        verbose_name="treść notatki",
                    ),
                ),
                (
                    "is_important",
                    models.BooleanField(
                        default=False,
                        verbose_name="ważne",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        db_index=True,
                        verbose_name="data dodania",
                    ),
                ),
                (
                    "text",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notes",
                        to="texts.text",
                        verbose_name="tekst",
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="text_notes",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="autor notatki",
                    ),
                ),
            ],
            options={
                "verbose_name": "notatka do tekstu",
                "verbose_name_plural": "notatki do tekstów",
                "ordering": ("-created_at", "-pk"),
            },
        ),
        migrations.CreateModel(
            name="Review",
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
                    "author_first_name",
                    models.CharField(
                        max_length=100,
                        verbose_name="imię autora",
                    ),
                ),
                (
                    "author_last_name",
                    models.CharField(
                        max_length=100,
                        verbose_name="nazwisko autora",
                    ),
                ),
                (
                    "title",
                    models.CharField(
                        max_length=255,
                        verbose_name="tytuł",
                    ),
                ),
                (
                    "genre",
                    models.CharField(
                        max_length=100,
                        verbose_name="gatunek",
                    ),
                ),
                (
                    "length",
                    models.PositiveIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                        ],
                        verbose_name="długość",
                    ),
                ),
                (
                    "content_warnings",
                    models.TextField(
                        blank=True,
                        verbose_name="trigger warningi",
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        max_length=254,
                        verbose_name="adres e-mail",
                    ),
                ),
                (
                    "phone_number",
                    models.CharField(
                        blank=True,
                        max_length=30,
                        verbose_name="numer telefonu",
                    ),
                ),
                (
                    "old_reviews",
                    models.BooleanField(
                        db_index=True,
                        default=False,
                        help_text=(
                            "Recenzja archiwalna, wyłączona ze statystyk "
                            "i bieżącego przydzielania pracy."
                        ),
                        verbose_name="stare recenzje",
                    ),
                ),
                (
                    "created_at",
                    models.DateField(
                        auto_now_add=True,
                        verbose_name="data wpisania",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "Nowy"),
                            ("in_review", "W trakcie oceny"),
                            ("accepted", "Przyjęty"),
                            ("rejected", "Odrzucony"),
                            ("withdrawn", "Wycofany"),
                        ],
                        db_index=True,
                        default="new",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                (
                    "decision_at",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="data decyzji",
                    ),
                ),
                (
                    "author_notified_at",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="data powiadomienia autora",
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="review_submissions",
                        to="authors.author",
                        verbose_name="autor w bazie",
                    ),
                ),
                (
                    "anthology",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviews",
                        to="texts.anthology",
                        verbose_name="nabór",
                    ),
                ),
                (
                    "copied_text",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="source_review",
                        to="texts.text",
                        verbose_name="tekst utworzony z recenzji",
                    ),
                ),
            ],
            options={
                "verbose_name": "recenzja",
                "verbose_name_plural": "recenzje",
                "ordering": ("-created_at", "-pk"),
            },
        ),
        migrations.CreateModel(
            name="Reviewers",
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
                    "general_notes",
                    models.TextField(
                        blank=True,
                        verbose_name="uwagi ogólne",
                    ),
                ),
                (
                    "review",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reviewers",
                        to="texts.review",
                        verbose_name="recenzja",
                    ),
                ),
            ],
            options={
                "verbose_name": "oceny recenzentów",
                "verbose_name_plural": "oceny recenzentów",
                "ordering": ("review__title", "pk"),
            },
        ),
        migrations.CreateModel(
            name="ReviewAssignment",
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
                    "position",
                    models.PositiveSmallIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(6),
                        ],
                        verbose_name="numer miejsca",
                    ),
                ),
                (
                    "opinion",
                    models.CharField(
                        choices=[
                            ("reading", "W CZYTANIU"),
                            ("yes", "TAK"),
                            ("yes_maybe", "TAK/MOŻE"),
                            ("maybe", "MOŻE"),
                            ("maybe_no", "MOŻE/NIE"),
                            ("no", "NIE"),
                        ],
                        default="reading",
                        max_length=10,
                        verbose_name="opinia",
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        verbose_name="uwagi recenzenta",
                    ),
                ),
                (
                    "assigned_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="data przydzielenia",
                    ),
                ),
                (
                    "opinion_changed_at",
                    models.DateField(
                        default=django.utils.timezone.localdate,
                        editable=False,
                        verbose_name="data zmiany opinii",
                    ),
                ),
                (
                    "review",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assignments",
                        to="texts.review",
                        verbose_name="recenzja",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="review_assignments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="recenzent",
                    ),
                ),
            ],
            options={
                "verbose_name": "przydział recenzenta",
                "verbose_name_plural": "przydziały recenzentów",
                "ordering": ("position", "pk"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("review", "user"),
                        name="unique_reviewer_per_review",
                    ),
                    models.UniqueConstraint(
                        fields=("review", "position"),
                        name="unique_reviewer_position",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            position__gte=1,
                            position__lte=6,
                        ),
                        name="review_position_valid_range",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            opinion__in=[
                                "reading",
                                "yes",
                                "yes_maybe",
                                "maybe",
                                "maybe_no",
                                "no",
                            ],
                        ),
                        name="review_opinion_valid",
                    ),
                ],
            },
        ),
    ]