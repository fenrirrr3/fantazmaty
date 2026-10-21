"""Szukajka WWW: katalog antologii i opowiadań dla wyszukiwarki na fantazmaty.pl oraz ustawienia szukajki."""

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0061_strona_www_koordynatorzy_reczni"),
        ("texts", "0043_anthology_discord_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteteamsettings",
            name="catalog_fetch_version",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=40,
                verbose_name="wersja ostatniego pobrania katalogu",
            ),
        ),
        migrations.AddField(
            model_name="siteteamsettings",
            name="catalog_fetched_at",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                null=True,
                verbose_name="ostatnie pobranie katalogu przez stronę",
            ),
        ),
        migrations.AddField(
            model_name="siteteamsettings",
            name="catalog_page_url",
            field=models.URLField(
                blank=True,
                help_text="Np. https://fantazmaty.pl/czytaj/szukaj/ – daje odnośnik „Odśwież stronę”. Puste – bez odnośnika.",
                max_length=500,
                validators=[django.core.validators.URLValidator(schemes=["https"])],
                verbose_name="adres szukajki na stronie",
            ),
        ),
        migrations.AlterField(
            model_name="siteteamsettings",
            name="last_fetch_version",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=40,
                verbose_name="wersja ostatniego pobrania ekipy",
            ),
        ),
        migrations.AlterField(
            model_name="siteteamsettings",
            name="last_fetched_at",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                null=True,
                verbose_name="ostatnie pobranie ekipy przez stronę",
            ),
        ),
        migrations.CreateModel(
            name="SiteCatalogAnthology",
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
                    models.CharField(max_length=255, verbose_name="nazwa na stronie"),
                ),
                (
                    "url",
                    models.URLField(
                        blank=True,
                        help_text="Strona antologii na fantazmaty.pl (https://…).",
                        max_length=500,
                        validators=[
                            django.core.validators.URLValidator(schemes=["https"])
                        ],
                        verbose_name="adres antologii",
                    ),
                ),
                (
                    "position",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="kolejność"
                    ),
                ),
                (
                    "is_enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Wyłączona antologia nie trafia do szukajki.",
                        verbose_name="włączona",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="ostatnia zmiana"),
                ),
                (
                    "anthology",
                    models.OneToOneField(
                        blank=True,
                        help_text="Powiązanie pozwala sprawdzać aktualizacje; puste dla antologii spoza CMS-u.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="site_catalog",
                        to="texts.anthology",
                        verbose_name="antologia w CMS-ie",
                    ),
                ),
            ],
            options={
                "verbose_name": "antologia w katalogu szukajki",
                "verbose_name_plural": "Szukajka WWW – katalog opowiadań",
                "ordering": ("position", "pk"),
            },
        ),
        migrations.CreateModel(
            name="SiteCatalogStory",
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
                ("title", models.CharField(max_length=255, verbose_name="tytuł")),
                (
                    "author",
                    models.CharField(
                        blank=True,
                        help_text="Tak jak na stronie, np. „Kazimierz Kyrcz Jr, Michał Walczak”.",
                        max_length=255,
                        verbose_name="autor",
                    ),
                ),
                (
                    "genres",
                    models.TextField(
                        blank=True,
                        help_text="Jeden gatunek w wierszu.",
                        verbose_name="gatunki",
                    ),
                ),
                (
                    "tags",
                    models.TextField(
                        blank=True,
                        help_text="Jeden tag w wierszu.",
                        verbose_name="tagi",
                    ),
                ),
                (
                    "position",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="kolejność"
                    ),
                ),
                (
                    "anthology",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stories",
                        to="core.sitecataloganthology",
                        verbose_name="antologia w katalogu",
                    ),
                ),
                (
                    "text",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="site_catalog_story",
                        to="texts.text",
                        verbose_name="tekst w CMS-ie",
                    ),
                ),
            ],
            options={
                "verbose_name": "opowiadanie w katalogu",
                "verbose_name_plural": "opowiadania w katalogu",
                "ordering": ("position", "pk"),
            },
        ),
    ]
