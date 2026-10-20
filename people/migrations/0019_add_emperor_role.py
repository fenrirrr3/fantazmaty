"""Rola honorowa „Bóg-Imperator” (bez uprawnień) przypisana osobie o wskazanym adresie e-mail.

Osobę szukamy po e-mailu profilu, a potem po e-mailu powiązanego konta (bez wielkości liter); gdy jej nie ma,
powstaje tylko rola. Nazwa jest zapisana na stałe – migracja nie importuje people.roles.
"""
from django.db import migrations

ROLE = "Bóg-Imperator"
EMAIL = "dawidwiktorski@gmail.com"


def add_emperor_role(apps, schema_editor):
    alias = schema_editor.connection.alias
    Role, Person = apps.get_model("people", "Role"), apps.get_model("people", "Person")
    role = Role.objects.using(alias).filter(name__iexact=ROLE).first() or Role.objects.using(alias).create(name=ROLE)
    people = Person.objects.using(alias).order_by("pk")
    person = people.filter(email__iexact=EMAIL).first() or people.filter(user__email__iexact=EMAIL).first()
    if person is not None:
        person.roles.add(role)


def remove_emperor_role(apps, schema_editor):
    alias = schema_editor.connection.alias
    for role in apps.get_model("people", "Role").objects.using(alias).filter(name__iexact=ROLE):
        role.people.clear()
        role.delete()


class Migration(migrations.Migration):
    dependencies = [("people", "0018_add_social_media_role")]
    operations = [migrations.RunPython(add_emperor_role, remove_emperor_role)]
