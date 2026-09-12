from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("people", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="previous_data",
            field=models.TextField(
                "poprzednie dane",
                blank=True,
                default="",
                help_text=(
                    "Dawne imiona i nazwiska, dodatkowe adresy e-mail oraz "
                    "wyjaśnienia rozbieżności w danych. Pole dostępne wyłącznie "
                    "w panelu administracyjnym."
                ),
            ),
        ),
    ]
