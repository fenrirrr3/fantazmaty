"""Combine an exact real-name contact with its pseudonym-only duplicate."""
import json
import unicodedata

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from illustrations.models import Illustration, Illustrator


def key(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def snapshot(person):
    return {field.name: getattr(person, field.attname) for field in person._meta.concrete_fields}


class Command(BaseCommand):
    help = 'Łączy wpis o pełnym imieniu i nazwisku z wpisem nazwanym pseudonimem; domyślnie tylko podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('--first-name', required=True)
        parser.add_argument('--last-name', required=True)
        parser.add_argument('--pseudonym', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        full_name = ' '.join((options['first_name'] + ' ' + options['last_name']).split())
        pseudonym = ' '.join(options['pseudonym'].split())
        if not all(value.strip() for value in (options['first_name'], options['last_name'], pseudonym)) or key(full_name) == key(pseudonym):
            raise CommandError('Podaj imię, nazwisko i odrębny pseudonim.')
        try:
            with transaction.atomic():
                contacts = list(Illustrator.objects.select_for_update().order_by('pk'))
                targets = [p for p in contacts if key(str(p)) == key(full_name)]
                if len(targets) != 1:
                    raise CommandError(f'Wymagany dokładnie jeden wpis „{full_name}”; znaleziono: {len(targets)}. Nic nie zapisano.')
                target = targets[0]
                if target.pseudonym.strip() and key(target.pseudonym) != key(pseudonym):
                    raise CommandError('Docelowy ilustrator ma już inny pseudonim. Nic nie zapisano.')
                sources = [p for p in contacts if p.pk != target.pk and
                           key(pseudonym) in {key(str(p)), key(p.pseudonym)}]
                if len(sources) > 1 or any(key(str(p)) != key(pseudonym) for p in sources):
                    raise CommandError('Pseudonim wskazuje kilka rekordów lub innego imiennego ilustratora. Nic nie zapisano.')
                source = sources[0] if sources else None
                if source and source.pseudonym.strip() and key(source.pseudonym) != key(pseudonym):
                    raise CommandError('Wpis do scalenia ma dodatkowy, inny pseudonim. Nic nie zapisano.')
                before = [snapshot(p) for p in (target, source) if p]
                target.pseudonym = pseudonym
                warnings = []
                if source:
                    for field in ('email', 'portfolio'):
                        current, incoming = getattr(target, field) or '', getattr(source, field) or ''
                        same = current.casefold() == incoming.casefold() if field == 'email' else current == incoming
                        if current and incoming and not same:
                            raise CommandError(f'Oba wpisy mają różne wartości pola „{field}”. Ustal właściwą wartość przed scaleniem. Nic nie zapisano.')
                        if not current:
                            setattr(target, field, incoming or (None if field == 'email' else ''))
                    if source.preferences.strip() and source.preferences.strip() != target.preferences.strip():
                        target.preferences = '\n\n'.join(filter(None, [target.preferences.strip(), source.preferences.strip()]))
                    target.covers = target.covers or source.covers
                    if source.is_active != target.is_active:
                        warnings.append('Zachowano aktywność docelowego wpisu imiennego.')
                linked = list(Illustration.objects.select_for_update().filter(illustrators=source).order_by('pk')) if source else []
                report = {
                    'mode': 'zapisano' if options['apply'] else 'podgląd – nic nie zapisano',
                    'before': before, 'after': snapshot(target),
                    'merged_id': source.pk if source else None,
                    'illustration_ids': [row.pk for row in linked],
                    'warnings': warnings,
                    'work': 'Statusy, daty i pozostałe dane ilustracji pozostają bez zmian.',
                }
                # Free the source's unique email only inside the same transaction.
                if source and source.email and target.email and source.email.casefold() == target.email.casefold():
                    source.email = None
                    source.save(update_fields=['email'])
                target.full_clean()
                if source or snapshot(target) != before[0]:
                    target.save()
                for row in linked:
                    # Use M2M managers so stale edit tokens are invalidated too.
                    row.illustrators.add(target)
                    row.illustrators.remove(source)
                if source:
                    source.delete()
                if not options['apply']:
                    transaction.set_rollback(True)
        except (ValidationError, IntegrityError) as error:
            raise CommandError(f'Nic nie zapisano: {error}') from error
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
