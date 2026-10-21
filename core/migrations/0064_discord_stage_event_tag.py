"""Treści reguły „Rozpoczęcie i zakończenie etapu” bez [zdarzenie] dostają ten tag.

Migracja 0057 zmieniła tylko domyślną treść; treść wpisana wcześniej w panelu (np. „Zakończono etap…”) ogłaszała
rozpoczęcia jak zakończenia. Pierwsze słowo „Zakończono/Rozpoczęto” (także „zakończenie”, „zakończony”…) zastępuje
[zdarzenie]; bez takiego słowa tag trafia na początek („[zdarzenie]: …”). Kod robi to samo przy wysyłce
(core.discord_notifications.with_event_tag), więc migracja tylko pokazuje to w panelu.
"""
import re

from django.db import migrations

EVENT_WORD_RE = re.compile(r'\b(?:zakończono|zakończenie|zakończony|rozpoczęto|rozpoczęcie|rozpoczęty)\b', re.IGNORECASE)


def forwards(apps, schema_editor):
    Template = apps.get_model('core', 'NotificationTemplate')
    db = schema_editor.connection.alias
    for template in Template.objects.using(db).filter(rule__trigger='stage_completed'):
        if '[zdarzenie]' in template.content.lower():
            continue
        content, count = EVENT_WORD_RE.subn('[zdarzenie]', template.content, count=1)
        template.content = content if count else f'[zdarzenie]: {template.content}'
        template.save(update_fields=['content'])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0063_szukajka_www_katalog_seed"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
