"""Replace reviewed tag variants only in the 43 stories from this import."""
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from texts.models import Anthology, Text

EXPECTED_SHA256 = '2b6d084957551d85316dd7593a6b6555b5aa17c6360557f9136dad5ee4533dba'


def parts(value):
    return [' '.join(v.split()) for v in re.split(r'[,\r\n]+', value or '') if v.strip()]


def merge(values):
    result, seen = [], set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return ', '.join(result)


def publish(path, report):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class Command(BaseCommand):
    help = 'Zamiana wariantów tagów z importu pięciu antologii; domyślnie podgląd bez zapisu.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('json_file')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', help='Nowy raport JSON, zawierający też wartości sprzed zmiany.')

    def handle(self, *args, **options):
        source = Path(options['json_file']).resolve()
        try:
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256:
                raise ValueError('Plik danych nie odpowiada tej wersji skryptu. Użyj obu plików z tej samej paczki.')
            payload = json.loads(raw)
            records = payload['records']
            if len(records) != 43 or len({(r['anthology'], r['title']) for r in records}) != 43:
                raise ValueError('Wymagane dokładnie 43 różne teksty z tej paczki.')
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f'Nie zapisano zmian: {exc}') from exc
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        path = Path(options['report'] or f'raport_zamiany_tagow_{stamp}.json').resolve()
        if path == source or path.suffix.lower() != '.json':
            raise CommandError('Raport musi być nowym plikiem JSON, innym niż plik danych.')
        try:
            with path.open('x', encoding='utf-8'):
                pass
        except OSError as exc:
            raise CommandError(f'Nie można utworzyć raportu; baza nietknięta: {exc}') from exc
        report = {'status': 'ROZPOCZĘTO', 'apply': options['apply'], 'time': stamp,
                  'input_sha256': EXPECTED_SHA256, 'rows': [], 'errors': [],
                  'matched': 0, 'planned': 0, 'written': 0, 'replaced_occurrences': 0}
        committed = False
        try:
            publish(path, report)
            if not connection.features.supports_transactions:
                raise ValueError('Baza nie obsługuje transakcji.')
            if options['apply'] and connection.vendor == 'mysql':
                with connection.cursor() as cursor:
                    cursor.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s', [Text._meta.db_table])
                    engine = cursor.fetchone()
                    if not engine or (engine[0] or '').upper() != 'INNODB':
                        raise ValueError('Tabela tekstów musi używać InnoDB.')
            with transaction.atomic():
                books = dict(Anthology.objects.values_list('pk', 'title'))
                qs = Text.objects.filter(title__in=[r['title'] for r in records]).order_by('pk').only('pk', 'title', 'anthology_id', 'tags', 'genre')
                if options['apply']:
                    qs = qs.select_for_update()
                index = defaultdict(list)
                for obj in qs:
                    index[(books.get(obj.anthology_id), obj.title)].append(obj)
                for item in records:
                    matches = index.get((item['anthology'], item['title']), [])
                    if len(matches) != 1:
                        report['errors'].append(f'{item["anthology"]} / {item["title"]}: liczba dokładnych dopasowań {len(matches)}, wymagane 1.')
                        continue
                    obj = matches[0]
                    report['matched'] += 1
                    # Each story has its own reviewed aliases; no global substitutions.
                    aliases = {key.casefold(): targets for key, targets in item['replacements'].items()}
                    converted, replaced = [], []
                    for value in parts(obj.tags):
                        targets = aliases.get(value.casefold())
                        if targets is None:
                            converted.append(value)
                        else:
                            converted.extend(targets)
                            replaced.append({'old': value, 'new': targets})
                    new_tags = merge(converted + item['tags'])
                    new_genre = merge(parts(obj.genre) + item['genres'])
                    for field, value in [('tags', new_tags), ('genre', new_genre)]:
                        Text._meta.get_field(field).clean(value, obj)
                    row = {'text_id': obj.pk, 'anthology': item['anthology'], 'title': obj.title,
                           'before': {'tags': obj.tags, 'genre': obj.genre},
                           'after': {'tags': new_tags, 'genre': new_genre}, 'replaced': replaced}
                    row['changed'] = row['before'] != row['after']
                    report['rows'].append(row)
                    report['planned'] += int(row['changed'])
                    report['replaced_occurrences'] += len(replaced)
                if report['errors']:
                    raise ValueError('Nie wszystkie teksty zostały jednoznacznie dopasowane. Cała operacja przerwana.')
                if options['apply']:
                    # Persist the complete before/after backup before the first UPDATE.
                    report['status'] = 'PLAN_PRZED_ZAPISEM'
                    publish(path, report)
                    for row in report['rows']:
                        if row['changed']:
                            fields = {k: v for k, v in row['after'].items() if v != row['before'][k]}
                            if Text.objects.filter(pk=row['text_id']).update(**fields) != 1:
                                raise ValueError('Rekord zniknął podczas zapisu.')
                    report['status'] = 'OCZEKUJE_NA_COMMIT'
                    publish(path, report)
                else:
                    report['status'] = 'KONTROLA_OK'
            if options['apply']:
                committed = True
                report.update(status='ZAPISANO', written=report['planned'])
        except Exception as exc:
            report['status'] = 'PRZERWANO'
            report['errors'].append(f'{type(exc).__name__}: {exc}')
        try:
            publish(path, report)
        except OSError as exc:
            self.stderr.write(json.dumps(report, ensure_ascii=False, indent=2))
            state = 'ZMIANY ZAPISANE' if committed else 'BEZ ZAPISU'
            raise CommandError(f'{state}, ale zapis końcowego raportu nie powiódł się: {exc}. Raport wypisano powyżej.') from exc
        self.stdout.write(f'{report["status"]}: dopasowano {report["matched"]}/43, zmienianych tekstów {report["planned"]}, zastępowanych wariantów {report["replaced_occurrences"]}, zapisano {report["written"]}.')
        self.stdout.write(f'Raport i wartości sprzed zmiany: {path}')
        if report['status'] == 'PRZERWANO':
            raise CommandError('Nie zapisano żadnych zmian. Szczegóły w raporcie.')
