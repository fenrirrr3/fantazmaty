"""Strona WWW: działy ekipy, podgrupy i ustawienia strony „Ekipa” na fantazmaty.pl."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0058_indeksy_historii_i_powiadomien"),
        ("people", "0020_rename_emperor_role"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteTeamSection",
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
                    "slug",
                    models.SlugField(
                        help_text="Kotwica na stronie: ekipa-<identyfikator>. Małe litery, cyfry i myślniki.",
                        unique=True,
                        verbose_name="identyfikator",
                    ),
                ),
                ("title", models.CharField(max_length=100, verbose_name="tytuł")),
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
                        help_text="Wyłączony dział nie trafia na stronę.",
                        verbose_name="włączony",
                    ),
                ),
                (
                    "show_count",
                    models.BooleanField(
                        default=True, verbose_name="liczba osób w nagłówku"
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True,
                        help_text='Akapit zamiast listy osób (np. Ilustracje). Dozwolone znaczniki: <a href="https://…">, <strong>, <em>; pozostałe są usuwane.',
                        verbose_name="notka",
                    ),
                ),
                (
                    "in_navigation",
                    models.BooleanField(
                        default=True, verbose_name="w nawigacji „Przejdź do działu”"
                    ),
                ),
                (
                    "navigation_label",
                    models.CharField(
                        blank=True,
                        help_text="Pusta – tytuł działu.",
                        max_length=100,
                        verbose_name="etykieta w nawigacji",
                    ),
                ),
                (
                    "coordinator_roles",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Osoby z tymi rolami trafiają do linii „Koordynacja”.",
                        related_name="+",
                        to="people.role",
                        verbose_name="role koordynatorów",
                    ),
                ),
            ],
            options={
                "verbose_name": "dział na stronie WWW",
                "verbose_name_plural": "Strona WWW – działy ekipy",
                "ordering": ("position", "pk"),
            },
        ),
        migrations.CreateModel(
            name="SiteTeamGroup",
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
                        blank=True,
                        help_text="Pusty przy jednej podgrupie – lista bez nagłówka.",
                        max_length=100,
                        verbose_name="tytuł podgrupy",
                    ),
                ),
                (
                    "position",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="kolejność"
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[("roles", "z ról"), ("manual", "ręcznie")],
                        default="roles",
                        max_length=10,
                        verbose_name="źródło",
                    ),
                ),
                (
                    "manual_names",
                    models.TextField(
                        blank=True,
                        help_text="Jedno imię i nazwisko w wierszu, w podanej kolejności.",
                        verbose_name="nazwiska",
                    ),
                ),
                (
                    "roles",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Aktywne, niezewnętrzne osoby z którąkolwiek z ról.",
                        related_name="+",
                        to="people.role",
                        verbose_name="role",
                    ),
                ),
                (
                    "section",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="groups",
                        to="core.siteteamsection",
                        verbose_name="dział",
                    ),
                ),
            ],
            options={
                "verbose_name": "podgrupa działu",
                "verbose_name_plural": "podgrupy działu",
                "ordering": ("position", "pk"),
            },
        ),
        migrations.CreateModel(
            name="SiteTeamSettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "lead_editor",
                    models.CharField(
                        default="Dawid Wiktorski",
                        max_length=200,
                        verbose_name="redaktor prowadzący",
                    ),
                ),
                (
                    "site_person",
                    models.CharField(
                        default="Dawid Wiktorski",
                        max_length=200,
                        verbose_name="strona internetowa",
                    ),
                ),
                (
                    "last_fetched_at",
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name="ostatnie pobranie przez stronę",
                    ),
                ),
                (
                    "last_fetch_version",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=40,
                        verbose_name="wersja ostatniego pobrania",
                    ),
                ),
            ],
            options={
                "verbose_name": "ustawienia strony WWW",
                "verbose_name_plural": "Strona WWW – ustawienia",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("id", 1)), name="single_site_team_settings"
                    )
                ],
            },
        ),
    ]
