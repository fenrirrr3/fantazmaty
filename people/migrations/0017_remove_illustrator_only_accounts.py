"""Usuwa konta i profile osób, które są wyłącznie ilustratorami i nigdy się nie zalogowały.

Ilustratorzy są obsługiwani przez spis (illustrations.Illustrator, migracja illustrations 0009).
Osoba jest usuwana tylko wtedy, gdy:
- jej jedyną rolą jest „Ilustrator” (osoby z innymi rolami zostają razem z dostępem do Ilustracji),
- nie ma konta albo konto nigdy się nie logowało i nie jest kontem administratora,
- w spisie jest jej wpis (ten sam e-mail albo jednoznacznie to samo imię i nazwisko / pseudonim),
- usunięcie nie dotknęłoby żadnych innych danych (przydziałów, historii, wpisów utworzonych przez konto).
W każdym innym przypadku osoba zostaje, a migracja wypisuje ją z powodem do ręcznego sprawdzenia.
Każda osoba jest usuwana osobno (savepoint), więc błąd jednej nie cofa pozostałych.
"""
import unicodedata

from django.db import migrations, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError

ROLE = 'ilustrator'


def key(value):
    return ' '.join(unicodedata.normalize('NFC', value or '').split()).casefold()


def directory_entry(Illustrator, emails, name):
    """Wpis spisu o tym e-mailu, potem jednoznacznie o tym imieniu i nazwisku / pseudonimie."""
    for email in emails:
        if email and Illustrator.objects.filter(email__iexact=email).exists():
            return True
    wanted = key(name)
    if not wanted:
        return False
    matches = [entry for entry in Illustrator.objects.all()
               if key(f'{entry.first_name} {entry.last_name}') == wanted
               or (entry.pseudonym and key(entry.pseudonym) == wanted)]
    return len(matches) == 1


def _has_rows(objs):
    return bool(len(objs)) if isinstance(objs, (list, set, tuple)) else objs.exists()


def other_data(using, objects):
    """Opis danych, które usunięcie by zmieniło poza samą osobą i kontem; pusty – bezpiecznie."""
    collector = Collector(using=using)
    try:
        for obj in objects:
            collector.collect([obj])
    except (ProtectedError, RestrictedError) as error:
        return sorted({obj._meta.verbose_name_plural for obj in error.args[1]})
    # Własne rekordy i ich powiązania M2M (role, grupy) nie są „innymi danymi”.
    own = {type(obj) for obj in objects}

    def foreign(model):
        return model not in own and not model._meta.auto_created

    found = {model._meta.verbose_name_plural for model, rows in collector.data.items() if foreign(model) and rows}
    found |= {qs.model._meta.verbose_name_plural for qs in collector.fast_deletes
              if foreign(qs.model) and _has_rows(qs)}
    found |= {field.model._meta.verbose_name_plural for (field, _), batches in collector.field_updates.items()
              if foreign(field.model) and any(_has_rows(batch) for batch in batches)}
    return sorted(str(name) for name in found)


def forward(apps, schema_editor):
    Person = apps.get_model('people', 'Person')
    Illustrator = apps.get_model('illustrations', 'Illustrator')
    using = schema_editor.connection.alias
    removed, kept = [], []
    candidates = Person.objects.using(using).filter(roles__name__iexact='Ilustrator').select_related('user').distinct()
    for person in candidates.order_by('pk'):
        roles = {name.casefold().strip() for name in person.roles.values_list('name', flat=True)}
        if roles != {ROLE} or person.legacy_coordinator_access or person.is_coordinator:
            continue
        user = person.user
        if user is not None and (user.last_login is not None or user.is_superuser or user.is_staff):
            continue
        name = f'{person.first_name} {person.last_name}'.strip() or (user.username if user else f'#{person.pk}')
        emails = [(person.email or '').strip(), (user.email or '').strip() if user else '']
        if not directory_entry(Illustrator, emails, f'{person.first_name} {person.last_name}'):
            kept.append(f'{name} (brak jednoznacznego wpisu w spisie ilustratorów)')
            continue
        objects = [person] + ([user] if user is not None else [])
        blocking = other_data(using, objects)
        if blocking:
            kept.append(f'{name} (powiązane dane: {", ".join(blocking)})')
            continue
        try:
            with transaction.atomic(using=using):
                person.delete()
                if user is not None:
                    user.delete()
        except Exception as error:  # noqa: BLE001 – jedna osoba nie może zatrzymać migracji
            kept.append(f'{name} (błąd usuwania: {error})')
            continue
        removed.append(name)
    # Podsumowanie zawsze, także gdy nie było nic do zrobienia.
    print(f'\n  Usunięte konta samych ilustratorów ({len(removed)}): ' + (', '.join(removed) or 'brak'))
    print(f'  Pozostawione do ręcznego sprawdzenia ({len(kept)}): ' + ('; '.join(kept) or 'brak'))


class Migration(migrations.Migration):

    dependencies = [
        ('people', '0016_remove_person_illustrator_fields'),
        ('illustrations', '0009_illustrators_into_directory'),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
