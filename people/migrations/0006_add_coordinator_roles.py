from django.db import migrations


COORDINATOR_ROLES = (
    "Koordynator redakcji",
    "Koordynator audiobooków",
    "Koordynator weryfikacji",
    "Koordynator ilustracji",
    "Koordynator recenzji",
    "Koordynator korekty",
    "Koordynator rekrutacji",
)


def add_coordinator_roles(apps, schema_editor):
    Role = apps.get_model("people", "Role")
    roles = Role.objects.using(schema_editor.connection.alias)

    for name in COORDINATOR_ROLES:
        if not roles.filter(name__iexact=name).exists():
            roles.create(name=name)


class Migration(migrations.Migration):
    dependencies = [("people", "0005_alter_person_email")]
    operations = [
        migrations.RunPython(
            add_coordinator_roles,
            migrations.RunPython.noop,
        ),
    ]
