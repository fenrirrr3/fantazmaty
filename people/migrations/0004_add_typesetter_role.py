from django.db import migrations


def add_typesetter(apps, schema_editor):
    Role = apps.get_model("people", "Role")
    roles = Role.objects.using(schema_editor.connection.alias)
    if not roles.filter(name__iexact="Składacz").exists():
        roles.create(name="Składacz")


class Migration(migrations.Migration):
    dependencies = [("people", "0003_delete_rekrutacja_rekrutacja")]
    operations = [migrations.RunPython(add_typesetter, migrations.RunPython.noop)]
