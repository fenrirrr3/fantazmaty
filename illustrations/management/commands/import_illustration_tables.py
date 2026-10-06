"""Import statuses, people, dates and excerpts from an illustration table."""
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from illustrations.models import Illustration, Illustrator
from illustrations.management.commands.import_illustration_credits import key
from texts.models import Anthology, Text


def snapshot(illustration):
    return {
        'status': illustration.status,
        'assigned_at': illustration.assigned_at.isoformat() if illustration.assigned_at else None,
        'illustrators': [str(p) for p in illustration.illustrators.all()] if illustration.pk else [],
        'illustrated_excerpt': illustration.illustrated_excerpt,
    }


class Command(BaseCommand):
    help = 'Import tabel Ilustracji: status, ilustratorzy, data i niepusty fragment; domyślnie podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('input')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_tabele_ilustracji.json')

    def handle(self, *args, **options):
        source = Path(options['input'])
        report_path = Path(options['report'])
        if report_path.suffix.lower() != '.json':
            report_path = Path(str(report_path) + '.json')
        if source.resolve() == report_path.resolve():
            raise CommandError('Raport musi mieć inną ścieżkę niż dane wejściowe.')
        report = {'mode': 'podgląd – nic nie zapisano', 'counts': {}, 'texts': [], 'conflicts': [],
                  'new_contacts': [], 'anthologies_marked_illustrated': [],
                  'scope': 'Status ilustracji, ilustratorzy, data przypisania, niepusty ilustrowany fragment. Autorzy, gatunki, tagi, ostrzeżenia, linki i uwagi pozostają bez zmian.'}
        try:
            raw = source.read_bytes()
            report['input_sha256'] = hashlib.sha256(raw).hexdigest()
            data = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('kind') != 'illustration_tables' or not isinstance(data.get('texts'), list) or not data['texts']:
                raise ValueError('Wymagany niepusty plik illustration_tables w wersji 1.')
            # Validate the entire payload before looking up or changing database records.
            rows = []
            seen = set()
            for number, row in enumerate(data['texts'], 1):
                if not isinstance(row, dict):
                    raise ValueError(f'Wiersz {number}: oczekiwano obiektu.')
                for field in ('anthology', 'title', 'status'):
                    if not isinstance(row.get(field), str) or not row[field].strip():
                        raise ValueError(f'Wiersz {number}: brak {field}.')
                identity = (key(row['anthology']), key(row['title']))
                if identity in seen:
                    raise ValueError(f'Wiersz {number}: powtórzony tekst.')
                seen.add(identity)
                names = row.get('illustrators')
                if not isinstance(names, list) or any(not isinstance(n, str) or not n.strip() for n in names):
                    raise ValueError(f'Wiersz {number}: nieprawidłowa lista ilustratorów.')
                names = list(dict.fromkeys(n.strip() for n in names))
                status = row['status']
                if status not in Illustration.Status.values or bool(names) != (status != Illustration.Status.UNASSIGNED):
                    raise ValueError(f'Wiersz {number}: status nie odpowiada przypisaniom.')
                assigned_at = date.fromisoformat(row['assigned_at']) if row.get('assigned_at') else None
                if status == Illustration.Status.UNASSIGNED and assigned_at:
                    raise ValueError(f'Wiersz {number}: nieprzypisana ilustracja nie może mieć daty przydziału.')
                excerpt = row.get('illustrated_excerpt', '')
                if not isinstance(excerpt, str):
                    raise ValueError(f'Wiersz {number}: fragment musi być tekstem.')
                rows.append({**row, 'illustrators': names, 'assigned_at': assigned_at, 'illustrated_excerpt': excerpt.strip()})
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text('{"mode":"sprawdzanie"}\n', encoding='utf-8')
            counts = Counter()
            with transaction.atomic():
                books = list(Anthology.objects.select_for_update().order_by('pk'))
                contacts = list(Illustrator.objects.select_for_update().order_by('pk'))
                stories_by_book = {}
                for number, row in enumerate(rows, 1):
                    label = {'row': number, 'anthology': row['anthology'], 'title': row['title']}
                    try:
                        matches = [b for b in books if key(b.title) == key(row['anthology'])]
                        if len(matches) != 1:
                            raise ValueError('Antologia musi wskazywać dokładnie jeden istniejący rekord.')
                        book = matches[0]
                        if book.is_novel or book.is_translated:
                            raise ValueError('Ten importer obsługuje zwykłe antologie.')
                        if book.pk not in stories_by_book:
                            stories_by_book[book.pk] = list(Text.objects.select_for_update().filter(anthology=book).order_by('pk'))
                        matches = [t for t in stories_by_book[book.pk] if key(t.title) == key(row['title'])]
                        if len(matches) != 1:
                            available = '; '.join(t.title for t in stories_by_book[book.pk])
                            raise ValueError('Nie znaleziono jednego zgodnego tekstu. Dostępne tytuły: ' + available)
                        story = matches[0]
                        illustration = Illustration.objects.select_for_update().filter(text=story).first()
                        created = illustration is None
                        illustration = illustration or Illustration(text=story)
                        before = snapshot(illustration)
                        if illustration.manual_illustrator_name or illustration.manual_illustrator_email:
                            raise ValueError('Istnieje ręczne przypisanie. Import nie usunie go automatycznie.')
                        existing = {key(name) for name in before['illustrators']}
                        incoming = {key(name) for name in row['illustrators']}
                        if existing - incoming:
                            raise ValueError('Tabela usuwałaby obecnego ilustratora: ' + ', '.join(before['illustrators']))
                        if before['status'] == Illustration.Status.DELIVERED and row['status'] != Illustration.Status.DELIVERED:
                            raise ValueError('Tabela cofałaby już oddaną ilustrację do wcześniejszego statusu.')
                        if before['status'] == Illustration.Status.IN_CORRECTIONS and row['status'] in (Illustration.Status.UNASSIGNED, Illustration.Status.ASSIGNED):
                            raise ValueError('Tabela cofałaby ilustrację z poprawek do wcześniejszego statusu.')
                        artists = []
                        for name in row['illustrators']:
                            matches = [p for p in contacts if key(str(p)) == key(name)]
                            if len(matches) > 1:
                                raise ValueError(f'Niejednoznaczny wpis ilustratora: {name}.')
                            if matches:
                                artist = matches[0]
                            else:
                                first, space, last = name.rpartition(' ')
                                artist = Illustrator(first_name=first if space else name, last_name=last if space else '', is_active=False)
                                artist.full_clean()
                                artist.save()
                                contacts.append(artist)
                                report['new_contacts'].append(str(artist))
                            artists.append(artist)
                        illustration.status = row['status']
                        if row['assigned_at'] is not None:
                            illustration.assigned_at = row['assigned_at']
                        if row['illustrated_excerpt']:
                            illustration.illustrated_excerpt = row['illustrated_excerpt']
                        after = {**snapshot(illustration), 'illustrators': sorted(str(p) for p in artists)}
                        changed = any(before[k] != after[k] for k in ('status', 'assigned_at', 'illustrated_excerpt')) or existing != incoming
                        if created or changed:
                            illustration.set_artists(artists, status=row['status'], preserve_assignment_date=True)
                        action = 'created' if created else 'updated' if changed else 'unchanged'
                        counts[action] += 1
                        report['texts'].append({**label, 'text_id': story.pk, 'action': action, 'before': before, 'after': snapshot(illustration)})
                        if not book.has_illustrations:
                            book.has_illustrations = True
                            book.save(update_fields=['has_illustrations'])
                            report['anthologies_marked_illustrated'].append(book.title)
                    except (ValueError, ValidationError) as error:
                        report['conflicts'].append({**label, 'error': '; '.join(error.messages) if isinstance(error, ValidationError) else str(error)})
                report['counts'] = dict(counts)
                if report['conflicts'] or not options['apply']:
                    transaction.set_rollback(True)
                if report['conflicts']:
                    report['mode'] = 'wycofano – nic nie zapisano'
                elif options['apply']:
                    report['mode'] = 'zapisano'
                report['counts_meaning'] = 'Zapisane operacje' if report['mode'] == 'zapisano' else 'Planowane operacje'
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except (OSError, ValueError, TypeError, ValidationError) as error:
            raise CommandError(f'Import przerwany: {error}') from error
        self.stdout.write(f"{report['mode']}. Wiersze: {len(rows)}. Konflikty: {len(report['conflicts'])}. Raport: {report_path}")
        if report['conflicts']:
            raise CommandError('Nie zapisano żadnych zmian. Sprawdź raport konfliktów.')
