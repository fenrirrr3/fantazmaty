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
            name="Role",
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
                    "name",
                    models.CharField(
                        max_length=100,
                        unique=True,
                        verbose_name="nazwa roli",
                    ),
                ),
            ],
            options={
                "verbose_name": "rola",
                "verbose_name_plural": "role",
                "ordering": ("name",),
            },
        ),
        migrations.CreateModel(
            name="Rekrutacja",
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
            ],
            options={
                "verbose_name": "rekrutacja",
                "verbose_name_plural": "rekrutacje",
            },
        ),
        migrations.CreateModel(
            name="Person",
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
                    "is_coordinator",
                    models.BooleanField(
                        default=False,
                        verbose_name="koordynator",
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
                    "dropbox_email",
                    models.EmailField(
                        blank=True,
                        max_length=254,
                        verbose_name="adres e-mail Dropboxa",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        verbose_name="wciąż w ekipie",
                    ),
                ),
                (
                    "leave_start_date",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="urlop od",
                    ),
                ),
                (
                    "leave_end_date",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="urlop do",
                    ),
                ),
                (
                    "leave_until_revoked",
                    models.BooleanField(
                        default=False,
                        verbose_name="urlop do odwołania",
                    ),
                ),
                (
                    "roles",
                    models.ManyToManyField(
                        blank=True,
                        related_name="people",
                        to="people.role",
                        verbose_name="role",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="person_profile",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="konto użytkownika",
                    ),
                ),
            ],
            options={
                "verbose_name": "osoba",
                "verbose_name_plural": "osoby",
                "ordering": (
                    "last_name",
                    "first_name",
                    "pk",
                ),
            },
        ),
        migrations.CreateModel(
            name="Vacation",
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
                    "start_date",
                    models.DateField(
                        verbose_name="urlop od",
                    ),
                ),
                (
                    "end_date",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="urlop do",
                    ),
                ),
                (
                    "until_revoked",
                    models.BooleanField(
                        default=False,
                        verbose_name="urlop do odwołania",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="data zgłoszenia",
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vacations",
                        to="people.person",
                        verbose_name="członek ekipy",
                    ),
                ),
            ],
            options={
                "verbose_name": "urlop",
                "verbose_name_plural": "urlopy",
                "ordering": (
                    "-start_date",
                    "-created_at",
                    "-pk",
                ),
            },
        ),
    ]