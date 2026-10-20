"""Rola honorowa „Bóg-Imperator” zmienia nazwę na „Imperator Ludzkości” (przypisania zostają).

Nazwy są zapisane na stałe – migracja nie importuje people.roles.
"""
from django.db import migrations

OLD, NEW = "Bóg-Imperator", "Imperator Ludzkości"


def rename(apps, schema_editor, source, target):
    alias = schema_editor.connection.alias
    Role = apps.get_model("people", "Role")
    old = Role.objects.using(alias).filter(name__iexact=source).first()
    if old is None:
        return
    existing = Role.objects.using(alias).filter(name__iexact=target).exclude(pk=old.pk).first()
    if existing is None:
        old.name = target
        old.save(update_fields=["name"])
        return
    # Rola o nowej nazwie już istnieje (np. dodana ręcznie) – przenosimy przypisania i usuwamy starą.
    existing.people.add(*old.people.all())
    old.people.clear()
    old.delete()


def forwards(apps, schema_editor):
    rename(apps, schema_editor, OLD, NEW)


def backwards(apps, schema_editor):
    rename(apps, schema_editor, NEW, OLD)


class Migration(migrations.Migration):
    dependencies = [("people", "0019_add_emperor_role")]
    operations = [migrations.RunPython(forwards, backwards)]
