"""Usuwa pozostałości dawnego importu miniatur Ekstraktów (powiązania miniatur, źródło „extracts-v1”).

Przed zmianą ograniczenia sprawdza, czy w bazie nie zostały miniatury bez długości – takie teksty
przestałyby spełniać ograniczenie. Wtedy migracja zatrzymuje się przed zmianą schematu i wypisuje tytuły.
"""

from django.db import migrations, models


def check_leftover_miniatures(apps, schema_editor):
    Text = apps.get_model("texts", "Text")
    leftovers = list(
        Text.objects.filter(import_source="extracts-v1", length__isnull=True, chapter_number__isnull=True)
        .values_list("pk", "title")[:20]
    )
    if leftovers:
        listed = ", ".join(f"#{pk} {title}" for pk, title in leftovers)
        raise RuntimeError(
            "W bazie zostały miniatury Ekstraktów z dawnego importu bez długości: "
            f"{listed}. Uzupełnij długość albo usuń te teksty i uruchom migrację ponownie."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("texts", "0038_anthologytask_not_applicable"),
    ]

    operations = [
        migrations.RunPython(check_leftover_miniatures, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="text",
            name="ordinary_text_requires_length",
        ),
        migrations.AddConstraint(
            model_name="text",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("chapter_number__isnull", False),
                    ("length__isnull", False),
                    ("import_source", "extract-volume-v2"),
                    _connector="OR",
                ),
                name="ordinary_text_requires_length",
            ),
        ),
        migrations.DeleteModel(
            name="ExtractTextLink",
        ),
    ]
