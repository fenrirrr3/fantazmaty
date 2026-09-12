from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Author",
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
                    "first_name",
                    models.CharField(
                        max_length=100,
                        verbose_name="imię",
                    ),
                ),
                (
                    "last_name",
                    models.CharField(
                        max_length=100,
                        verbose_name="nazwisko",
                    ),
                ),
                (
                    "pseudonym",
                    models.CharField(
                        blank=True,
                        max_length=100,
                        verbose_name="pseudonim",
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        max_length=254,
                        unique=True,
                        verbose_name="adres e-mail",
                    ),
                ),
                (
                    "has_contract",
                    models.BooleanField(
                        default=False,
                        verbose_name="umowa",
                    ),
                ),
                (
                    "contact",
                    models.BooleanField(
                        default=True,
                        verbose_name="kontakt",
                    ),
                ),
                (
                    "is_blacklisted",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "Przed dodaniem zgłoszenia tego autora do recenzji "
                            "należy wyświetlić ostrzeżenie."
                        ),
                        verbose_name="czarna lista",
                    ),
                ),
            ],
            options={
                "verbose_name": "autor",
                "verbose_name_plural": "autorzy",
                "ordering": (
                    "last_name",
                    "first_name",
                    "pk",
                ),
            },
        ),
        migrations.CreateModel(
            name="AuthorNote",
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
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        db_index=True,
                        verbose_name="data dodania",
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notes",
                        to="authors.author",
                        verbose_name="autor",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="author_notes",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="osoba dodająca",
                    ),
                ),
            ],
            options={
                "verbose_name": "notatka o autorze",
                "verbose_name_plural": "notatki o autorach",
                "ordering": (
                    "-created_at",
                    "-pk",
                ),
            },
        ),
    ]