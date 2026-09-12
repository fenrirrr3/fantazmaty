import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("people", "0001_initial"),
        ("texts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Illustration",
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
                    "trigger_warnings",
                    models.TextField(
                        blank=True,
                        verbose_name="trigger warnings",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("unassigned", "Nieprzypisane"),
                            ("assigned", "Przypisane"),
                            ("delivered", "Oddane"),
                            ("in_corrections", "W trakcie poprawek"),
                        ],
                        db_index=True,
                        default="unassigned",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                (
                    "assigned_at",
                    models.DateField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name="data przypisania",
                    ),
                ),
                (
                    "story_url",
                    models.URLField(
                        blank=True,
                        max_length=500,
                        verbose_name="link do opowiadania",
                    ),
                ),
                (
                    "illustrated_excerpt",
                    models.TextField(
                        blank=True,
                        verbose_name="ilustrowany fragment",
                    ),
                ),
                (
                    "text",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="illustration",
                        to="texts.text",
                        verbose_name="tytuł",
                    ),
                ),
                (
                    "illustrator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="illustrations",
                        to="people.person",
                        verbose_name="ilustrator",
                    ),
                ),
            ],
            options={
                "verbose_name": "ilustracja",
                "verbose_name_plural": "ilustracje",
                "ordering": (
                    "text__anthology__title",
                    "text__title",
                    "pk",
                ),
            },
        ),
        migrations.CreateModel(
            name="CoverProposal",
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
                    "illustration_author",
                    models.CharField(
                        max_length=255,
                        verbose_name="autor ilustracji",
                    ),
                ),
                (
                    "illustration_url",
                    models.URLField(
                        max_length=500,
                        verbose_name="link do ilustracji",
                    ),
                ),
                (
                    "submitted_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="data zgłoszenia",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Oczekujące"),
                            ("inquiry_sent", "Wysłane zapytanie"),
                            ("rejected", "Odrzucone"),
                            ("no_contact", "Brak kontaktu"),
                            ("approved", "Zgoda"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                (
                    "status_changed_at",
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name="data zmiany statusu",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="cover_proposals",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="zgłoszone przez",
                    ),
                ),
            ],
            options={
                "verbose_name": "propozycja ilustracji okładkowej",
                "verbose_name_plural": "propozycje ilustracji okładkowych",
                "ordering": ("-submitted_at", "-pk"),
            },
        ),
    ]