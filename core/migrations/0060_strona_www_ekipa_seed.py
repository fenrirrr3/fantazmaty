"""Seed działów strony „Ekipa” odtwarzający obecną stronę fantazmaty.pl/o-projekcie/ekipa/.

Role są tworzone ``get_or_create`` po nazwie, żeby migracja działała też na pustej bazie.
Cofnięcie usuwa wszystkie działy i ustawienia (ról nie rusza).
"""
from django.db import migrations

ILLUSTRATIONS_NOTE = ('Osoby, które do tej pory z\u00a0nami współpracowały, znajdziesz na stronach ilustrowanych '
                      '<a href="https://fantazmaty.pl/czytaj/antologie/">antologii</a>.')

# (slug, tytuł, show_count, role koordynatorów, notka, grupy); grupa: (tytuł, źródło, role albo nazwiska).
# Etykieta w nawigacji inna niż tytuł: NAVIGATION_LABELS.
NAVIGATION_LABELS = {'audio': 'Audio'}
SECTIONS = (
    ('redakcja', 'Redakcja', True, ('Koordynator redakcji',), '', (('', 'roles', ('Redaktor',)),)),
    ('korekta', 'Korekta', True, ('Koordynator korekty',), '', (('', 'roles', ('Korektor',)),)),
    ('korekta-poskladowa', 'Korekta poskładowa', True, (), '', (('', 'roles', ('Korektor poskładowy',)),)),
    ('weryfikacja', 'Weryfikacja', True, ('Koordynator weryfikacji',), '', (('', 'roles', ('Weryfikator',)),)),
    ('recenzje', 'Recenzje', True, ('Koordynator recenzji',), '', (('', 'roles', ('Recenzent',)),)),
    ('ilustracje', 'Ilustracje', False, ('Koordynator ilustracji',), ILLUSTRATIONS_NOTE, ()),
    ('grafika-sklad', 'Grafika i skład', False, (), '',
     (('Grafika', 'roles', ('Grafik',)), ('Skład', 'roles', ('Składacz',)))),
    ('audio', 'Dział audio', False, ('Koordynator audiobooków',), '', (
        ('Lektorzy', 'manual', ('Aleksandra Czyżewska', 'Izabella Dobrowolska', 'Sebastian Filipowicz',
                                'Rafał Flejter', 'Jakub Kloch', 'Paweł Roicki', 'M.\u00a0Stella Sit',
                                'Karolina Stachacz-Bartosik', 'Ela Zińczuk')),
        ('Dźwiękowcy', 'manual', ('Teodor Laskowski',)),
        ('Korektorzy', 'manual', ('Jadwiga Glaza', 'Anna Koral', 'Agnieszka Nowak', 'Agnieszka Rzepecka',
                                  'Aneta Stawiszyńska-Marciniak', 'Katarzyna Twarduś')),
    )),
)


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Role = apps.get_model('people', 'Role')
    Section = apps.get_model('core', 'SiteTeamSection')
    Group = apps.get_model('core', 'SiteTeamGroup')
    Settings = apps.get_model('core', 'SiteTeamSettings')
    if Section.objects.using(db).exists():
        return

    def role(name):
        return Role.objects.using(db).get_or_create(name=name)[0]

    for position, (slug, title, show_count, coordinators, note, groups) in enumerate(SECTIONS, start=1):
        section = Section.objects.using(db).create(slug=slug, title=title, position=position, show_count=show_count,
                                                   note=note, navigation_label=NAVIGATION_LABELS.get(slug, ''))
        section.coordinator_roles.set(role(name) for name in coordinators)
        for group_position, (group_title, source, items) in enumerate(groups, start=1):
            group = Group.objects.using(db).create(
                section=section, title=group_title, position=group_position, source=source,
                manual_names='\n'.join(items) if source == 'manual' else '')
            if source == 'roles':
                group.roles.set(role(name) for name in items)
    Settings.objects.using(db).get_or_create(pk=1)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    apps.get_model('core', 'SiteTeamSection').objects.using(db).all().delete()
    apps.get_model('core', 'SiteTeamSettings').objects.using(db).all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0059_strona_www_ekipa'),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
