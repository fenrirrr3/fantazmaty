"""Seed strony „Słuchaj” z ``core/site_listen_seed.json`` (dane publiczne z fantazmaty.pl/sluchaj/: 11 antologii,
116 nagrań z czasami) i dopasowanie do antologii oraz audiobooków CMS-u.

Każde nagranie zachowuje identyfikator karty ze strony; czasy YouTube i HearThis trafiają do ``SiteListenRecording``.
Niedopasowane nagrania zostają bez audiobooka (ostrzeżenie w zakładce „Słuchaj WWW”, ponowne dopasowanie przyciskiem).
Adres strony w ustawieniach: https://fantazmaty.pl/sluchaj/ (jeśli pusty). Cofnięcie usuwa antologie i nagrania.
"""
import json
from pathlib import Path

from django.db import migrations

SEED = Path(__file__).resolve().parent.parent / 'site_listen_seed.json'
PAGE_URL = 'https://fantazmaty.pl/sluchaj/'


def forwards(apps, schema_editor):
    from core.site_listen import link_listen_to_cms

    db = schema_editor.connection.alias
    Entry = apps.get_model('core', 'SiteListenAnthology')
    Recording = apps.get_model('core', 'SiteListenRecording')
    Settings = apps.get_model('core', 'SiteTeamSettings')
    settings = Settings.objects.using(db).get_or_create(pk=1)[0]
    if not settings.listen_page_url:
        Settings.objects.using(db).filter(pk=1).update(listen_page_url=PAGE_URL)
    if Entry.objects.using(db).exists():
        return
    data = json.loads(SEED.read_text(encoding='utf-8'))
    for position, item in enumerate(data['antologie'], start=1):
        entry = Entry.objects.using(db).create(slug=item['id'], title=item['nazwa'], cover_url=item['okladka'],
                                               position=position)
        Recording.objects.using(db).bulk_create([
            Recording(listen_anthology=entry, card_id=record['id'], youtube_duration=record['czasYoutube'],
                      hearthis_duration=record['czasHearthis'], source_title=record['tytul'],
                      source_author=record['autor'], source_youtube=record['youtube'],
                      source_hearthis=record['hearthis'])
            for record in item['nagrania']])
    link_listen_to_cms(apps)


def backwards(apps, schema_editor):
    apps.get_model('core', 'SiteListenAnthology').objects.using(schema_editor.connection.alias).all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0065_sluchaj_www'),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
