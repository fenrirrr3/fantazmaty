"""Reguła „Zakończenie etapu”: jedna, wyłączona, bez kanału; nietknięta domyślna treść dostaje [autor] i [osoba].

Treści zmienione w panelu zostają bez zmian. Kanału nie ustawiamy – wybiera go administrator.
"""
from django.db import migrations

OLD = 'Zakończono etap „[etap]”: „[tytuł]” ([antologia]).'
NEW = 'Zakończono etap „[etap]”: „[tytuł]” – [autor] ([antologia]). Wykonanie: [osoba].'


def set_template(apps, schema_editor, old, new):
    Rule = apps.get_model('core', 'NotificationRule')
    Template = apps.get_model('core', 'NotificationTemplate')
    db = schema_editor.connection.alias
    rule, created = Rule.objects.using(db).get_or_create(trigger='stage_completed', defaults={'enabled': False})
    templates = list(Template.objects.using(db).filter(rule=rule).values_list('content', flat=True))
    if created:
        Template.objects.using(db).create(rule=rule, content=new)
    elif templates == [old]:
        Template.objects.using(db).filter(rule=rule).update(content=new)


def forwards(apps, schema_editor):
    set_template(apps, schema_editor, OLD, NEW)


def backwards(apps, schema_editor):
    set_template(apps, schema_editor, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0050_discord_waiting_reminders"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
