# Czas wpisu dziennika aktywności przychodzi z kolejki lokalnej (flush_activity) zamiast auto_now_add.
# Bez zmiany schematu bazy – wartość domyślna jest ustawiana w Pythonie.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0069_guidelines_help_pdf"),
    ]

    operations = [
        migrations.AlterField(
            model_name="useractivity",
            name="created_at",
            field=models.DateTimeField(
                db_index=True, default=django.utils.timezone.now, editable=False, verbose_name="czas"
            ),
        ),
    ]
