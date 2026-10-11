"""Słownik zamian Odkurzacza: początkowe pozycje.

Lista jest kopią wbudowanej listy Odkurzacza z chwili utworzenia słownika
(USER_WORD_EXACT, USER_WORD_PATTERNS i „komedia dell’arte”). Migracja nie
importuje jej z kodu aplikacji, aby późniejsze zmiany stałej jej nie zmieniły.
"""
from django.db import migrations

INITIAL_REPLACEMENTS = (
    ('word', 'niechcąco', 'niechcący'),
    ('word', 'zaczym', 'za czym'),
    ('word', 'władnie', 'władny'),
    ('word', 'niewiadomo', 'nie wiadomo'),
    ('word', 'terefere', 'tere-fere'),
    ('word', 'inąd', 'skądinąd'),
    ('word', 'wszechczasów', 'wszech czasów'),
    ('word', 'zapewnie', 'zapewne'),
    ('pattern', r'\bpickup(?P<ending>owi|ów|om|ami|ach|em|ie|a|y|u)?\b', r'pikap\g<ending>'),
    ('pattern', r'\btonicem\b', 'tonikiem'),
    ('pattern', r'\btonicy\b', 'toniki'),
    ('pattern', r'\btonic(?P<ending>owi|ów|om|ami|ach|iem|a|u|i)?\b', r'tonik\g<ending>'),
    ('pattern', r'\bmentlik(?P<ending>owi|ów|om|ami|ach|iem|a|u|i)?\b', r'mętlik\g<ending>'),
    ('pattern', r'\b(?P<neg>nie)?przekonywując(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b', r'\g<neg>przekonując\g<ending>'),
    ('pattern', r'\b(?P<neg>nie)?oddziaływując(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b', r'\g<neg>oddziałując\g<ending>'),
    ('pattern', r'\b(?P<neg>nie)?zadawalając(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b', r'\g<neg>zadowalając\g<ending>'),
    ('pattern', r'\bmiałcz(?P<ending>eć|ę|ysz|y|ymy|ycie|ą|ałem|ałam|ałeś|ałaś|ał|ała|ało|ałyśmy|ałyście|ały|eliśmy|eliście|eli|ałbym|ałbyś|ałby|ałabym|ałabyś|ałaby|ałoby|elibyśmy|elibyście|eliby|ałybyśmy|ałybyście|ałyby|my|cie|ąc|ący|ąca|ące|ącą|ącego|ącej|ącemu|ącym|ącymi|ących|enie|enia|eniu|eniem|eń|eniom|eniami|eniach)?\b', r'miaucz\g<ending>'),
    ('pattern', r'\bwymyśle(?P<ending>ć|liśmy|liście|li|libyśmy|libyście|liby)\b', r'wymyśli\g<ending>'),
    ('pattern', r'\bdomyśle(?P<ending>ć|liśmy|liście|li|libyśmy|libyście|liby)\b', r'domyśli\g<ending>'),
    ('pattern', r'\bdopatrzeć\b', 'dopatrzyć'),
    ('pattern', r'\bdopatrza(?P<ending>łem|łam|łeś|łaś|ł|ła|ło|łyśmy|łyście|ły|łbym|łbyś|łby|łabym|łabyś|łaby|łoby|łybyśmy|łybyście|łyby)\b', r'dopatrzy\g<ending>'),
    ('pattern', r'\bdopatrze(?P<ending>liśmy|liście|li|libyśmy|libyście|liby)\b', r'dopatrzy\g<ending>'),
    ('pattern', r'\bkarnister\b', 'kanister'),
    ('pattern', r'\bkarnistr(?P<ending>owi|ów|om|ami|ach|em|ze|a|y)\b', r'kanistr\g<ending>'),
    ('pattern', r'\bmassmedi(?P<ending>ami|ach|ów|om|a)\b', r'mass medi\g<ending>'),
    ('pattern', r'\beuropejczyk(?P<ending>owi|ów|om|ami|ach|iem|a|u)?\b', r'Europejczyk\g<ending>'),
    ('pattern', r'\beuropejczycy\b', 'Europejczycy'),
    ('pattern', r'\bazjat(?P<ending>ami|ach|ów|om|a|y|ę|ą|o)\b', r'Azjat\g<ending>'),
    ('pattern', r'\bazjaci(?P<ending>e)?\b', r'Azjaci\g<ending>'),
    ('pattern', r'\b(?P<noun>komedi(?:ami|ach|om|a|i|ę|ą|o|e))(?P<space>[ \t\u00a0\u202f]+)(?P<term>dellarte)\b', r'\g<noun>\g<space>dell’arte'),
)


def fill(apps, schema_editor):
    ReplacementEntry = apps.get_model('core', 'ReplacementEntry')
    if ReplacementEntry.objects.exists():
        return
    ReplacementEntry.objects.bulk_create(
        ReplacementEntry(kind=kind, source=source, target=target, position=(index + 1) * 10)
        for index, (kind, source, target) in enumerate(INITIAL_REPLACEMENTS)
    )


def empty(apps, schema_editor):
    apps.get_model('core', 'ReplacementEntry').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0046_program_preferences_replacements'),
    ]

    operations = [
        migrations.RunPython(fill, empty),
    ]
