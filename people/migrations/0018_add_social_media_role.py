"""Rola „Social Media” (dostęp do zakładki Promocja → Social Media) istnieje od razu po migrate.

Nazwa jest zapisana na stałe – migracja nie importuje people.roles, bo stała może się później zmienić.
"""
from django.db import migrations

ROLE = "Social Media"


def add_social_media_role(apps, schema_editor):
    Role = apps.get_model("people", "Role")
    roles = Role.objects.using(schema_editor.connection.alias)
    if not roles.filter(name__iexact=ROLE).exists():
        roles.create(name=ROLE)


class Migration(migrations.Migration):
    dependencies = [("people", "0017_remove_illustrator_only_accounts")]
    operations = [migrations.RunPython(add_social_media_role, migrations.RunPython.noop)]
