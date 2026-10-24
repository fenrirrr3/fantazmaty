"""Słownik zamian Odkurzacza: lista właściciela z października 2026.

Usuwa pozycję pickup → pikap, rozszerza fleksję pięciu pozycji (-ywuj- we wszystkich formach, czasownik
„zadawalać”, przedrostki „miałczeć”, Azjatka) i dopisuje nowe pozycje na końcu słownika. Pozycja jest
usuwana albo zmieniana tylko wtedy, gdy w bazie ma dokładnie domyślną postać (rodzaj, zapis, zamiana) –
zmiany koordynatorów zostają. Nowa pozycja nie jest dopisywana, gdy słownik ma już taki zapis. Dane są
kopią listy z kodu (odkurzacz.DEFAULT_REPLACEMENTS) – migracja jej nie importuje.
"""
from django.db import migrations

# Pozycja usuwana (postać z 0047_initial_replacements).
REMOVED = (
    ('pattern', r'\bpickup(?P<ending>owi|ów|om|ami|ach|em|ie|a|y|u)?\b', r'pikap\g<ending>'),
)
# (postać domyślna dotąd, nowa postać).
CHANGED = (
    (
        ('pattern', r'\b(?P<neg>nie)?przekonywując(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b', r'\g<neg>przekonując\g<ending>'),
        ('pattern', r'\b(?P<neg>nie)?przekonywuj(?P<ending>\w*)', r'\g<neg>przekonuj\g<ending>'),
    ),
    (
        ('pattern', r'\b(?P<neg>nie)?oddziaływując(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b', r'\g<neg>oddziałując\g<ending>'),
        ('pattern', r'\b(?P<neg>nie)?oddziaływuj(?P<ending>\w*)', r'\g<neg>oddziałuj\g<ending>'),
    ),
    (
        ('pattern', r'\b(?P<neg>nie)?zadawalając(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b', r'\g<neg>zadowalając\g<ending>'),
        ('pattern', r'\b(?P<neg>nie)?zadawala(?P<ending>\w*)', r'\g<neg>zadowala\g<ending>'),
    ),
    (
        ('pattern', r'\bmiałcz(?P<ending>eć|ę|ysz|y|ymy|ycie|ą|ałem|ałam|ałeś|ałaś|ał|ała|ało|ałyśmy|ałyście|ały|eliśmy|eliście|eli|ałbym|ałbyś|ałby|ałabym|ałabyś|ałaby|ałoby|elibyśmy|elibyście|eliby|ałybyśmy|ałybyście|ałyby|my|cie|ąc|ący|ąca|ące|ącą|ącego|ącej|ącemu|ącym|ącymi|ących|enie|enia|eniu|eniem|eń|eniom|eniami|eniach)?\b', r'miaucz\g<ending>'),
        ('pattern', r'\b(?P<prefix>za|po|roz)?miałcz(?P<ending>\w*)', r'\g<prefix>miaucz\g<ending>'),
    ),
    (
        ('pattern', r'\bazjat(?P<ending>ami|ach|ów|om|a|y|ę|ą|o)\b', r'Azjat\g<ending>'),
        ('pattern', r'\bazjat(?P<ending>ami|ach|ów|om|a|y|ę|ą|o|k(?:a|i|ę|ą|o|om|ami|ach)|ce|ek)\b', r'Azjat\g<ending>'),
    ),
)
# Nowe pozycje, w kolejności listy w kodzie (słowa, potem wzorce).
ADDED = (
    ('word', 'dellarte', 'dell’arte'),
    ('word', 'księżami', 'księżmi'),
    ('word', 'oldboi', 'oldbojów'),
    ('word', 'nabazgram', 'nabazgrzę'),
    ('word', 'nabazgrasz', 'nabazgrzesz'),
    ('word', 'nabazgra', 'nabazgrze'),
    ('word', 'nabazgramy', 'nabazgrzemy'),
    ('word', 'nabazgracie', 'nabazgrzecie'),
    ('word', 'nabazgrają', 'nabazgrzą'),
    ('pattern', r'\beuropej(?P<ending>k(?:a|i|ę|ą|o|om|ami|ach)|ce|ek)\b', r'Europej\g<ending>'),
    ('pattern', r'\bkreol(?P<ending>a|owi|em|u|e|owie|ów|om|ami|ach|k(?:a|i|ę|ą|o|om|ami|ach)|ce|ek)?\b', r'Kreol\g<ending>'),
    ('pattern', r'\bmula(?P<ending>t|ta|towi|tem|cie|ci|tów|tom|tami|tach|ty|tk(?:a|i|ę|ą|o|om|ami|ach)|tce|tek)\b', r'Mula\g<ending>'),
    ('pattern', r'\babsorbc(?P<ending>\w+)', r'absorpc\g<ending>'),
    ('pattern', r'\bmiting(?P<ending>\w*)', r'mityng\g<ending>'),
    ('pattern', r'\bcent(?P<ending>k(?:a|i|ę|ą|o|om|ami|ach|owan(?:y|a|e|ego|emu|ej|ą|ym|ymi|ych|i))|ce|ek)\b', r'cęt\g<ending>'),
    ('pattern', r'\bkurestw(?P<ending>\w*)', r'kurewstw\g<ending>'),
    ('pattern', r'\bindywidulan(?P<ending>\w*)', r'indywidualn\g<ending>'),
    ('pattern', r'\bprzeciwskaza(?P<ending>\w*)', r'przeciwwskaza\g<ending>'),
    ('pattern', r'\bśredniozaawansowan(?P<ending>\w*)', r'średnio zaawansowan\g<ending>'),
    ('pattern', r'\bgrekokatolic(?P<ending>[kc]\w*)', r'greckokatolic\g<ending>'),
    ('pattern', r'\bwyksztu(?P<ending>[sś]\w*)', r'wykrztu\g<ending>'),
    ('pattern', r'\bzarzadz(?P<ending>\w*)', r'zarządz\g<ending>'),
    ('pattern', r'\b(?P<neg>nie)?zcentraliz(?P<ending>\w*)', r'\g<neg>scentraliz\g<ending>'),
    ('pattern', r'\b(?P<neg>nie)?skategoryz(?P<ending>\w*)', r'\g<neg>kategoryz\g<ending>'),
    ('pattern', r'\b(?P<prefix>za)?przysięgł(?P<ending>em|eś|by|bym|byś)?\b', r'\g<prefix>przysiągł\g<ending>'),
    ('pattern', r'\bniedowierza(?P<ending>ć|m|sz|my|cie|ją|j|jmy|jcie|jąc|ł\w*|l\w*)?\b', r'nie dowierza\g<ending>'),
    ('pattern', r'\bniemniejs(?P<ending>z\w*|i)\b', r'nie mniejs\g<ending>'),
    ('pattern', r'\bnienaj(?P<ending>gorsz\w*|gorsi|gorzej|lepsz\w*|lepsi|lepiej)\b', r'nie naj\g<ending>'),
)


def _exact(entries, item):
    # Porównanie w Pythonie: MySQL porównuje napisy bez rozróżniania wielkości liter.
    return [entry for entry in entries if (entry.kind, entry.source, entry.target) == item]


def forwards(apps, schema_editor):
    ReplacementEntry = apps.get_model('core', 'ReplacementEntry')
    entries = list(ReplacementEntry.objects.all())
    for item in REMOVED:
        for entry in _exact(entries, item):
            entry.delete()
    taken = {(entry.kind, entry.source.casefold()) for entry in entries}
    for old, new in CHANGED:
        if (new[0], new[1].casefold()) in taken:
            continue
        for entry in _exact(entries, old):
            entry.source, entry.target = new[1], new[2]
            entry.save()
    taken = {(entry.kind, entry.source.casefold()) for entry in ReplacementEntry.objects.all()}
    position = max(ReplacementEntry.objects.values_list('position', flat=True), default=0)
    created = []
    for kind, source, target in ADDED:
        if (kind, source.casefold()) not in taken:
            position += 10
            created.append(ReplacementEntry(kind=kind, source=source, target=target, position=position))
    ReplacementEntry.objects.bulk_create(created)


def backwards(apps, schema_editor):
    ReplacementEntry = apps.get_model('core', 'ReplacementEntry')
    entries = list(ReplacementEntry.objects.all())
    for item in ADDED:
        for entry in _exact(entries, item):
            entry.delete()
    for old, new in CHANGED:
        for entry in _exact(entries, new):
            entry.source, entry.target = old[1], old[2]
            entry.save()
    taken = {(entry.kind, entry.source.casefold()) for entry in ReplacementEntry.objects.all()}
    for kind, source, target in REMOVED:
        if (kind, source.casefold()) not in taken:
            ReplacementEntry.objects.create(kind=kind, source=source, target=target, position=90)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0066_sluchaj_www_seed'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
