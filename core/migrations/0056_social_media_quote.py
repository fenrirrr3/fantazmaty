"""Cytaty z opowiadań w zakładce Social Media."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0055_mailbox_split_mailing_purpose"),
        ("texts", "0042_review_not_read_opinion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SocialMediaQuote",
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
                ("content", models.TextField(max_length=2000, verbose_name="cytat")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("free", "Wolne"),
                            ("in_progress", "W przygotowaniu"),
                            ("posted", "Wrzucone"),
                        ],
                        db_index=True,
                        default="free",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                (
                    "posted_on",
                    models.DateField(
                        default=django.utils.timezone.localdate,
                        verbose_name="data wrzucenia",
                    ),
                ),
                (
                    "added_by_name",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=200,
                        verbose_name="dodał(a) – podpis",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="utworzono"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="zmieniono"),
                ),
                (
                    "added_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="dodał(a)",
                    ),
                ),
                (
                    "text",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="social_quotes",
                        to="texts.text",
                        verbose_name="tekst",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="zmienił(a)",
                    ),
                ),
            ],
            options={
                "verbose_name": "social media – cytat",
                "verbose_name_plural": "Social Media – cytaty",
                "ordering": ("created_at", "pk"),
            },
        ),
    ]
