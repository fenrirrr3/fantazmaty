"""Scala dwa wpisy spisu lektorów i montażystów: źródło znika, jego nagrania przechodzą na cel."""
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from core.audiobook_merge import merge_audio_contributors
from core.audiobook_models import AudioContributor


def snapshot(person):
    return {field.name: getattr(person, field.attname) for field in person._meta.concrete_fields}


def find(name, pk, role, flag):
    """Dokładnie jeden wpis po ID albo po dokładnej nazwie; inaczej czytelny błąd."""
    if pk is not None:
        rows = list(AudioContributor.objects.select_for_update().filter(pk=pk))
        label = f'o ID {pk}'
    else:
        # Porównanie w Pythonie: collation MySQL nie rozróżnia wielkości liter.
        rows = [row for row in AudioContributor.objects.select_for_update().filter(name=name).order_by('pk') if row.name == name]
        label = f'„{name}”'
    if len(rows) != 1:
        hint = f' (ID: {", ".join(str(row.pk) for row in rows)}) – wskaż wpis przez --{flag}-id' if rows else ''
        raise CommandError(f'Wymagany dokładnie jeden wpis ({role}) {label}; znaleziono: {len(rows)}{hint}. Nic nie zapisano.')
    return rows[0]


class Command(BaseCommand):
    help = ('Scala wpis lektora/montażysty (źródło) z innym wpisem (cel): przepina nagrania, uzupełnia pusty '
            'e-mail celu i usuwa źródło. --dry-run pokazuje skutki bez zapisu.')

    def add_arguments(self, parser):
        parser.add_argument('source', nargs='?', help='Dokładna nazwa wpisu do usunięcia.')
        parser.add_argument('target', nargs='?', help='Dokładna nazwa wpisu, który zostaje.')
        parser.add_argument('--source-id', type=int)
        parser.add_argument('--target-id', type=int)
        parser.add_argument('--dry-run', action='store_true', help='Tylko podgląd – nic nie zapisuje.')

    def handle(self, *args, **options):
        if options['source_id'] is not None and options['target'] is None:
            # Jedna nazwa obok --source-id oznacza cel.
            options['target'], options['source'] = options['source'], None
        for role in ('source', 'target'):
            if (options[role] is None) == (options[f'{role}_id'] is None):
                raise CommandError(f'Podaj {"źródło" if role == "source" else "cel"} nazwą albo przez --{role}-id (jedno z dwóch).')
        try:
            with transaction.atomic():
                source = find(options['source'], options['source_id'], 'źródło', 'source')
                target = find(options['target'], options['target_id'], 'cel', 'target')
                if source.pk == target.pk:
                    raise CommandError('Źródło i cel to ten sam wpis. Nic nie zapisano.')
                warnings = []
                if source.email and target.email and source.email != target.email:
                    warnings.append(f'Cel ma już e-mail {target.email}; e-mail źródła {source.email} zostanie pominięty.')
                if source.is_active != target.is_active:
                    warnings.append(f'Zachowano aktywność celu ({"aktywny" if target.is_active else "nieaktywny"}).')
                report = {'mode': 'podgląd – nic nie zapisano' if options['dry_run'] else 'zapisano',
                          'source': snapshot(source), 'target_before': snapshot(target)}
                report['moved'] = merge_audio_contributors(target, [source])
                target.refresh_from_db()
                report['target_after'] = snapshot(target)
                report['warnings'] = warnings
                if options['dry_run']:
                    transaction.set_rollback(True)
        except (ValidationError, IntegrityError) as error:
            raise CommandError(f'Nic nie zapisano: {error}') from error
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
