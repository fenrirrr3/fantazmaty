"""Import reviewed story metadata into existing texts; preview unless --apply."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import unicodedata

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from texts.models import Anthology, Text
from texts.vocabulary import canonicalize


SCHEMA = 'fantazmaty-production-tags-v1'


def match_key(value):
    # Deliberately do not remove accents, punctuation or words.
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def load_rows(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError('Nieobsługiwany format danych importu.')
    rows = data.get('texts')
    if not isinstance(rows, list) or not rows or data.get('count') != len(rows):
        raise ValueError('Nieprawidłowa lista tekstów lub liczba rekordów.')
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Każdy tekst musi być obiektem JSON.')
        for field in ('anthology', 'title', 'genre', 'tags'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f'Brak poprawnego pola {field}.')
        aliases = row.get('title_aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(x, str) or not x.strip() for x in aliases):
            raise ValueError('Nieprawidłowe warianty tytułu.')
        for title in {match_key(row['title']), *(match_key(x) for x in aliases)}:
            key = (match_key(row['anthology']), title)
            if key in keys:
                raise ValueError(f"Powtórzone lub nakładające się tytuły: {row['anthology']} / {row['title']}.")
            keys.add(key)
    return rows, hashlib.sha256(raw).hexdigest()


def write_report(handle, report):
    handle.seek(0)
    json.dump(report, handle, ensure_ascii=False, indent=2)
    handle.write('\n')
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


class Command(BaseCommand):
    help = 'Podgląd lub atomowe nadpisanie gatunków i tagów istniejących tekstów z JSON.'

    def add_arguments(self, parser):
        parser.add_argument('input_file')
        parser.add_argument('--apply', action='store_true', help='Zapisz zmiany. Bez tej opcji tylko podgląd.')
        parser.add_argument('--report', help='Nowy plik raportu JSON; istniejący plik nie zostanie nadpisany.')

    def handle(self, *args, **options):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        report_path = Path(options['report'] or f'raport_tagi_{stamp}.json')
        report = {'schema': SCHEMA, 'mode': 'rozpoczęto', 'input_sha256': None,
                  'counts': {'planned': 0, 'updated': 0, 'unchanged': 0},
                  'texts': [], 'errors': []}
        try:
            # Reserve a fresh audit file before any database work.
            handle = report_path.open('x', encoding='utf-8')
        except OSError as error:
            raise CommandError(f'Nie zapisano zmian. Nie można utworzyć nowego raportu {report_path}: {error}') from error
        committed = False
        with handle:
            try:
                write_report(handle, report)
                rows, digest = load_rows(options['input_file'])
                report['input_sha256'] = digest
                with transaction.atomic():
                    books_query = Anthology.objects.all().order_by('pk')
                    if options['apply']:
                        books_query = books_query.select_for_update()
                    required = {match_key(row['anthology']) for row in rows}
                    books = {key: [] for key in required}
                    for book in books_query:
                        key = match_key(book.title)
                        if key in books:
                            books[key].append(book)
                    matched_books = {}
                    for key, candidates in books.items():
                        if len(candidates) != 1:
                            report['errors'].append(f'Antologia „{key}”: znaleziono {len(candidates)} rekordów zamiast jednego.')
                        elif candidates[0].is_novel or candidates[0].is_translated:
                            report['errors'].append(f'Antologia „{candidates[0].title}” jest powieścią lub tłumaczeniem; ten import dotyczy zwykłych opowiadań.')
                        else:
                            matched_books[key] = candidates[0]
                    texts_query = Text.objects.filter(anthology_id__in=[b.pk for b in matched_books.values()]).order_by('pk')
                    if options['apply']:
                        texts_query = texts_query.select_for_update()
                    index = {}
                    for obj in texts_query:
                        index.setdefault((obj.anthology_id, match_key(obj.title)), []).append(obj)
                    plan = []
                    used_ids = set()
                    for row in rows:
                        book = matched_books.get(match_key(row['anthology']))
                        if book is None:
                            continue
                        accepted = {match_key(row['title']), *(match_key(x) for x in row.get('title_aliases', []))}
                        matches = {obj.pk: obj for title in accepted for obj in index.get((book.pk, title), [])}
                        if len(matches) != 1:
                            report['errors'].append(f"{book.title} / {row['title']}: znaleziono {len(matches)} rekordów zamiast jednego.")
                            continue
                        obj = next(iter(matches.values()))
                        if obj.pk in used_ids:
                            report['errors'].append(f'Tekst ID {obj.pk} dopasowano więcej niż raz.')
                            continue
                        used_ids.add(obj.pk)
                        after = {field: canonicalize(row[field], kind, register=False)
                                 for field, kind in (('tags', 'tag'), ('genre', 'genre'))}
                        for field, value in after.items():
                            Text._meta.get_field(field).clean(value, obj)
                        before = {field: getattr(obj, field) for field in after}
                        changed = before != after
                        entry = {'id': obj.pk, 'anthology_id': book.pk, 'anthology': book.title,
                                 'title': obj.title, 'input_title': row['title'],
                                 'matched_alias': match_key(obj.title) != match_key(row['title']),
                                 'before': before, 'after': after, 'changed': changed}
                        report['texts'].append(entry)
                        plan.append((obj, row, entry))
                    report['counts']['planned'] = sum(entry['changed'] for _, _, entry in plan)
                    report['counts']['unchanged'] = sum(not entry['changed'] for _, _, entry in plan)
                    if report['errors']:
                        raise ValueError('Nie wszystkie teksty mają jednoznaczne dopasowanie. Szczegóły w raporcie.')
                    report['mode'] = 'podgląd – nic nie zapisano'
                    if options['apply']:
                        for obj, row, entry in plan:
                            after = {field: canonicalize(row[field], kind, register=True)
                                     for field, kind in (('tags', 'tag'), ('genre', 'genre'))}
                            # Abort if the vocabulary changed while the plan was being built.
                            if after != entry['after']:
                                raise ValueError('Słownik zmienił się podczas importu. Uruchom ponownie podgląd.')
                            if entry['changed']:
                                updated = Text.objects.filter(pk=obj.pk, **entry['before']).update(**after)
                                if updated != 1:
                                    raise ValueError(f'Tekst ID {obj.pk} zmienił się podczas importu.')
                        report['mode'] = 'przygotowano zapis – oczekuje na zatwierdzenie transakcji'
                    # Preserve before/after while a failed report write can still roll back.
                    write_report(handle, report)
                committed = bool(options['apply'])
                if committed:
                    report['mode'] = 'zapisano'
                    report['counts']['updated'] = report['counts']['planned']
                write_report(handle, report)
            except Exception as error:
                if committed:
                    # Never incorrectly claim a successful commit was rolled back.
                    self.stderr.write('Dane zapisano, ale aktualizacja końcowego raportu nie powiodła się. Poprzednie wartości są w raporcie przygotowania transakcji.')
                    raise CommandError(str(error)) from error
                report['mode'] = 'wycofano – nic nie zapisano'
                report['counts']['updated'] = 0
                if not report['errors']:
                    report['errors'].append(str(error))
                try:
                    write_report(handle, report)
                except OSError as report_error:
                    self.stderr.write(f'Nie udało się zapisać raportu: {report_error}')
                    self.stderr.write(json.dumps(report, ensure_ascii=False))
                raise CommandError(f'Nie zapisano zmian: {error}\nRaport: {report_path}') from error
        verb = 'Zmieniono' if committed else 'Do zmiany'
        self.stdout.write(f"{verb}: {report['counts']['planned']}; bez zmian: {report['counts']['unchanged']}. Dopasowano: {len(report['texts'])}. Raport: {report_path}")
