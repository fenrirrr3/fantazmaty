"""Seed katalogu szukajki z ``core/site_catalog_seed.json`` (dane publiczne z fantazmaty.pl/czytaj/szukaj/:
27 antologii, 297 opowiadań) i próba dopasowania wpisów do antologii oraz tekstów CMS-u.

Niedopasowane wpisy zostają bez powiązania (można je powiązać później przyciskiem w zakładce „Szukajka WWW”).
Cofnięcie usuwa cały katalog.
"""
import json
from pathlib import Path

from django.db import migrations

SEED = Path(__file__).resolve().parent.parent / 'site_catalog_seed.json'


def forwards(apps, schema_editor):
    from core.site_catalog import link_catalog_to_cms

    db = schema_editor.connection.alias
    CatalogAnthology = apps.get_model('core', 'SiteCatalogAnthology')
    CatalogStory = apps.get_model('core', 'SiteCatalogStory')
    Settings = apps.get_model('core', 'SiteTeamSettings')
    if CatalogAnthology.objects.using(db).exists():
        return
    data = json.loads(SEED.read_text(encoding='utf-8'))
    for position, item in enumerate(data['antologie'], start=1):
        anthology = CatalogAnthology.objects.using(db).create(title=item['nazwa'], url=item['url'], position=position)
        CatalogStory.objects.using(db).bulk_create([
            CatalogStory(anthology=anthology, title=story['tytul'], author=story['autor'],
                         genres='\n'.join(story['gatunki']), tags='\n'.join(story['tagi']), position=number)
            for number, story in enumerate(item['opowiadania'], start=1)])
    Settings.objects.using(db).get_or_create(pk=1)
    link_catalog_to_cms(apps)


def backwards(apps, schema_editor):
    apps.get_model('core', 'SiteCatalogAnthology').objects.using(schema_editor.connection.alias).all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0062_szukajka_www_katalog'),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
