"""Wszyscy ilustratorzy w spisie (kartotece).

1. Dane ilustratorów zapisane jeszcze przy kontach zespołu (osoby z rolą „Ilustrator”
   albo z wypełnionym portfolio/preferencjami/okładkami) trafiają do spisu: istniejący wpis
   (ten sam e-mail albo jednoznacznie to samo imię i nazwisko / pseudonim) jest uzupełniany
   tylko w pustych polach, w przeciwnym razie powstaje nowy wpis. Konta zostają.
2. Ilustratorzy wpisani ręcznie przy ilustracji stają się wpisami spisu i są przypisani
   do tej ilustracji; pola „ręcznie” znikają.

Nic nie jest nadpisywane; migracja jest idempotentna.
"""
import unicodedata

from django.db import migrations
from django.db.models import Q


def key(value):
    return ' '.join(unicodedata.normalize('NFC', value or '').split()).casefold()


def split_name(full):
    full = ' '.join((full or '').split())
    first, _, last = full.rpartition(' ')
    return (first, last) if first else (full, '')


AMBIGUOUS = object()


def find(Illustrator, email, names):
    """Wpis o tym e-mailu, potem jednoznacznie o tym imieniu i nazwisku / pseudonimie;
    None – brak, AMBIGUOUS – kilka pasujących wpisów."""
    if email:
        match = Illustrator.objects.filter(email__iexact=email).first()
        if match:
            return match
    wanted = {key(n) for n in names if key(n)}
    matches = [p for p in Illustrator.objects.all()
               if key(f'{p.first_name} {p.last_name}') in wanted or (p.pseudonym and key(p.pseudonym) in wanted)]
    if len(matches) > 1:
        return AMBIGUOUS
    return matches[0] if matches else None


def email_free(Illustrator, email, pk=None):
    return email and not Illustrator.objects.filter(email__iexact=email).exclude(pk=pk).exists()


def forward(apps, schema_editor):
    Person = apps.get_model('people', 'Person')
    Illustrator = apps.get_model('illustrations', 'Illustrator')
    Illustration = apps.get_model('illustrations', 'Illustration')
    counts = {'osoby_uzupełnione': 0, 'osoby_nowe': 0, 'ręczne_przypisane': 0, 'ręczne_nowe': 0}
    ambiguous = []

    people = Person.objects.filter(Q(roles__name__iexact='Ilustrator') | ~Q(illustrator_portfolio='')
                                   | ~Q(illustrator_preferences='') | Q(illustrator_covers=True)).distinct().order_by('pk')
    role_ids = set(Person.objects.filter(roles__name__iexact='Ilustrator').values_list('pk', flat=True))
    for person in people:
        email = (person.email or '').strip()
        entry = find(Illustrator, email, [f'{person.first_name} {person.last_name}'])
        if entry is AMBIGUOUS:
            # Kilka wpisów o tym nazwisku – bez e-maila nie wiadomo, który to; nic nie dopisujemy.
            ambiguous.append(f'{person.first_name} {person.last_name}'.strip())
            continue
        if entry is None:
            Illustrator.objects.create(
                first_name=person.first_name, last_name=person.last_name,
                email=email if email_free(Illustrator, email) else None,
                portfolio=person.illustrator_portfolio, preferences=person.illustrator_preferences,
                covers=person.illustrator_covers, is_active=person.illustrator_active and person.pk in role_ids)
            counts['osoby_nowe'] += 1
            continue
        changed = []
        if not entry.portfolio and person.illustrator_portfolio:
            entry.portfolio = person.illustrator_portfolio
            changed.append('portfolio')
        if not entry.preferences and person.illustrator_preferences:
            entry.preferences = person.illustrator_preferences
            changed.append('preferences')
        if person.illustrator_covers and not entry.covers:
            entry.covers = True
            changed.append('covers')
        if not entry.email and email_free(Illustrator, email, entry.pk):
            entry.email = email
            changed.append('email')
        if changed:
            entry.save(update_fields=changed)
            counts['osoby_uzupełnione'] += 1

    for illustration in Illustration.objects.exclude(manual_illustrator_name='').order_by('pk'):
        name = ' '.join(illustration.manual_illustrator_name.split())
        email = illustration.manual_illustrator_email.strip()
        entry = find(Illustrator, email, [name])
        if entry is None or entry is AMBIGUOUS:
            first, last = split_name(name)
            entry = Illustrator.objects.create(
                first_name=first, last_name=last, email=email if email_free(Illustrator, email) else None,
                is_active=illustration.status in ('assigned', 'in_corrections'))
            counts['ręczne_nowe'] += 1
        illustration.illustrators.add(entry)
        illustration.manual_illustrator_name = ''
        illustration.manual_illustrator_email = ''
        illustration.save(update_fields=['manual_illustrator_name', 'manual_illustrator_email'])
        counts['ręczne_przypisane'] += 1
    if any(counts.values()):
        print('\n  Spis ilustratorów: ' + ', '.join(f'{k}={v}' for k, v in counts.items()))
    if ambiguous:
        print('  Osoby zespołu z kilkoma pasującymi wpisami w spisie (do ręcznego sprawdzenia): '
              + ', '.join(sorted(set(ambiguous))))


class Migration(migrations.Migration):

    dependencies = [
        ('illustrations', '0008_illustrator_pseudonym'),
        ('people', '0015_retire_generic_coordinator'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
        migrations.RemoveField(model_name='illustration', name='manual_illustrator_name'),
        migrations.RemoveField(model_name='illustration', name='manual_illustrator_email'),
    ]
