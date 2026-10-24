"""Słownik zamian Odkurzacza: usunięcie dwóch pozycji z 0067 sprzecznych z reformą pisowni 2026.

Od 1 stycznia 2026 r. „nie” ze stopniem wyższym i najwyższym przymiotników i przysłówków pisze się łącznie
(niemniejszy, nienajlepszy, nienajgorzej), więc zamiany na zapis rozdzielny (sprzed reformy) znikają.
Pozycja jest usuwana tylko wtedy, gdy w bazie ma dokładnie domyślną postać (rodzaj, zapis, zamiana) – zmiany
koordynatorów zostają. Cofnięcie dopisuje pozycje z powrotem na końcu słownika (gdy nie ma już takiego zapisu).
"""
from django.db import migrations

# Postać z 0067_replacements_owner_list.
REMOVED = (
    ('pattern', r'\bniemniejs(?P<ending>z\w*|i)\b', r'nie mniejs\g<ending>'),
    ('pattern', r'\bnienaj(?P<ending>gorsz\w*|gorsi|gorzej|lepsz\w*|lepsi|lepiej)\b', r'nie naj\g<ending>'),
)


def forwards(apps, schema_editor):
    ReplacementEntry = apps.get_model('core', 'ReplacementEntry')
    # Porównanie w Pythonie: MySQL porównuje napisy bez rozróżniania wielkości liter.
    for entry in ReplacementEntry.objects.filter(kind='pattern'):
        if (entry.kind, entry.source, entry.target) in REMOVED:
            entry.delete()


def backwards(apps, schema_editor):
    ReplacementEntry = apps.get_model('core', 'ReplacementEntry')
    taken = {(entry.kind, entry.source.casefold()) for entry in ReplacementEntry.objects.all()}
    position = max(ReplacementEntry.objects.values_list('position', flat=True), default=0)
    for kind, source, target in REMOVED:
        if (kind, source.casefold()) not in taken:
            position += 10
            ReplacementEntry.objects.create(kind=kind, source=source, target=target, position=position)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0067_replacements_owner_list'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
