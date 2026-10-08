"""Import historical illustration credits, with an atomic preview by default."""
import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


def key(value):
    value = unicodedata.normalize('NFC', value)
    value = value.translate(str.maketrans({'„': '"', '”': '"', '“': '"', '’': "'", '‘': "'", '—': '–'}))
    return ' '.join(value.casefold().split())


class Command(BaseCommand):
    help = 'Importuje historyczne ilustracje; bez --apply wykonuje tylko podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('input')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_ilustracje.json')

    def handle(self, *args, **options):
        report_path = Path(options['report'])
        if report_path.suffix.lower() != '.json':
            report_path = Path(str(report_path) + '.json')
        if report_path.resolve() == Path(options['input']).resolve():
            raise CommandError('Plik raportu nie może być plikiem danych.')
        report = {'mode': 'podgląd – nic nie zapisano', 'counts': {}, 'texts': [],
                  'conflicts': [], 'warnings': [], 'new_contacts': [], 'skipped': []}
        counts = Counter()
        try:
            raw = Path(options['input']).read_bytes()
            report['input_sha256'] = hashlib.sha256(raw).hexdigest()
            data = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('texts'), list):
                raise ValueError('Oczekiwano schema_version=1 i listy texts.')
            aliases = data.get('illustrator_aliases', {})
            if not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in aliases.items()):
                raise ValueError('Niepoprawny słownik illustrator_aliases.')
            aliases = {key(k): v.strip() for k, v in aliases.items()}
            # Verify the report destination before a transaction can commit.
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps({'mode': 'rozpoczęto sprawdzanie danych'}, ensure_ascii=False), encoding='utf-8')
            with transaction.atomic():
                books = list(Anthology.objects.select_for_update().order_by('pk'))
                contacts = list(Illustrator.objects.select_for_update().order_by('pk'))
                seen = set()
                stories_by_book = {}
                for index, row in enumerate(data['texts'], 1):
                    label = {'row': index}
                    try:
                        if not isinstance(row, dict):
                            raise ValueError('Wiersz musi być obiektem JSON.')
                        for field in ('anthology', 'title', 'author'):
                            if not isinstance(row.get(field), str) or not row[field].strip():
                                raise ValueError(f'Brak pola {field}.')
                        label.update(anthology=row['anthology'], title=row['title'])
                        if row.get('skip_reason'):
                            report['skipped'].append({**label, 'reason': row['skip_reason']})
                            counts['skipped'] += 1
                            continue
                        names = row.get('illustrators')
                        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
                            raise ValueError('Wymagana niepusta lista illustratorów.')
                        names = list(dict.fromkeys(aliases.get(key(n), n.strip()) for n in names))
                        matches = [b for b in books if key(b.title) == key(row['anthology'])]
                        if len(matches) != 1:
                            raise ValueError('Antologia musi wskazywać dokładnie jeden istniejący rekord.')
                        book = matches[0]
                        if book.is_novel or book.is_translated:
                            raise ValueError('Import dotyczy zwykłych antologii, nie powieści ani tłumaczeń.')
                        if book.pk not in stories_by_book:
                            stories_by_book[book.pk] = list(Text.objects.select_for_update().filter(anthology=book).order_by('pk').prefetch_related('authors'))
                        stories = stories_by_book[book.pk]
                        matches = [t for t in stories if key(t.title) == key(row['title'])]
                        if len(matches) != 1:
                            raise ValueError('Tytuł musi wskazywać dokładnie jeden tekst w tej antologii. Dostępne tytuły: ' + '; '.join(t.title for t in stories))
                        story = matches[0]
                        if story.pk in seen:
                            raise ValueError('Ten tekst podano więcej niż raz w pliku.')
                        seen.add(story.pk)
                        actual_names = {key(n) for author in story.authors.all() for n in (author.display_name, f'{author.first_name} {author.last_name}')}
                        if not all(key(n) in actual_names for n in row['author'].split(',')):
                            report['warnings'].append({**label, 'warning': 'Autor w stopce różni się od danych CMS. Dopasowano jednoznaczną parę antologia + tytuł; autorów nie zmieniono.',
                                                       'source_author': row['author'], 'database_authors': [a.display_name for a in story.authors.all()]})
                        artists = []
                        for name in names:
                            matches = [p for p in contacts if key(name) in {key(str(p)), key(p.pseudonym)}]
                            if len(matches) > 1:
                                raise ValueError(f'Kilka wpisów ilustratora {name}; najpierw uporządkuj spis w adminie.')
                            if matches:
                                artist = matches[0]
                            else:
                                parts = name.rsplit(' ', 1)
                                artist = Illustrator(first_name=parts[0], last_name=parts[1] if len(parts) > 1 else '', is_active=False)
                                artist.full_clean()
                                artist.save()
                                contacts.append(artist)
                                report['new_contacts'].append(str(artist))
                                counts['new_contacts'] += 1
                            artists.append(artist)
                        illustration = Illustration.objects.select_for_update().filter(text=story).first()
                        wanted = {p.pk for p in artists}
                        existing = set(illustration.illustrators.values_list('pk', flat=True)) if illustration else set()
                        if illustration and (illustration.manual_illustrator_name or illustration.manual_illustrator_email):
                            raise ValueError('Istnieje ręczne przypisanie ilustratora. Import nie nadpisze go automatycznie.')
                        if illustration and (existing - wanted or illustration.status not in (Illustration.Status.UNASSIGNED, Illustration.Status.DELIVERED)):
                            raise ValueError('Istnieją inne przypisania lub trwająca praca. Import nie nadpisze ich automatycznie.')
                        if illustration and existing == wanted and illustration.status == Illustration.Status.DELIVERED:
                            action = 'unchanged'
                        else:
                            action = 'updated' if illustration else 'created'
                            if illustration is None:
                                illustration = Illustration(text=story)
                            illustration.set_artists(artists, status=Illustration.Status.DELIVERED, preserve_assignment_date=True)
                        if not book.has_illustrations:
                            book.has_illustrations = True
                            book.save(update_fields=['has_illustrations'])
                            counts['anthologies_marked_illustrated'] += 1
                        counts[action] += 1
                        report['texts'].append({**label, 'text_id': story.pk, 'database_title': story.title,
                                                'illustrators': [str(a) for a in artists], 'action': action,
                                                'assigned_at': str(illustration.assigned_at) if illustration.assigned_at else None})
                        if book.status != Anthology.Status.READY:
                            report['warnings'].append({**label, 'warning': 'Antologia nie ma statusu Gotowa: będzie widoczna także przy włączonym ukrywaniu wydanych.'})
                    except (ValueError, ValidationError) as error:
                        report['conflicts'].append({**label, 'error': '; '.join(error.messages) if isinstance(error, ValidationError) else str(error)})
                report['counts'] = dict(counts)
                if report['conflicts'] or not options['apply']:
                    transaction.set_rollback(True)
                elif options['apply']:
                    report['mode'] = 'zapisano'
                if report['conflicts']:
                    report['mode'] = 'wycofano – nic nie zapisano'
                report['counts_meaning'] = 'Planowane operacje' if report['mode'] != 'zapisano' else 'Zapisane operacje'
                report['dates'] = 'Nieznane daty pozostają puste; istniejące daty są zachowane.'
                report['contacts'] = 'Nowe wpisy bez kont, bez adresów e-mail, nieaktywne; istniejąca aktywność pozostaje bez zmian.'
                # Save the report before commit: an unwritable report rolls back the import.
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except (OSError, ValueError, ValidationError) as error:
            raise CommandError(f'Import przerwany: {error}') from error
        self.stdout.write(f"{report['mode']}. Teksty: {sum(counts[k] for k in ('created', 'updated', 'unchanged'))}; konflikty: {len(report['conflicts'])}. Raport: {report_path}")
        if report['conflicts']:
            raise CommandError('Nie zapisano żadnych zmian. Szczegóły konfliktów znajdują się w raporcie.')
