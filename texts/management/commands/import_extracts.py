"""python manage.py import_extracts data/ekstrakty.md [--apply]."""
from collections import Counter
from pathlib import Path
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from authors.models import Author
from texts.models import Extract
from texts.extract_data import read_markdown, split_list, normalize_key, author_names

FIELDS = ('full_name', 'email', 'phone_number', 'title', 'submission_dates', 'recruitment', 'accepted_titles', 'rejected_titles')


class Command(BaseCommand):
    help = 'Import tabeli Markdown: jeden autor w jednej antologii. Domyślnie próba bez zapisu.'

    def add_arguments(self, parser):
        parser.add_argument('file', type=Path)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='Zapisz cały import atomowo.')
        mode.add_argument('--dry-run', action='store_true', help='Sprawdź import i wycofaj wszystkie zmiany (domyślne).')
        parser.add_argument('--update-existing', action='store_true', help='Zastąp dane istniejącego udziału danymi z pliku. Bez tej opcji różnice zatrzymują cały import.')

    def handle(self, *args, **options):
        try:
            records = read_markdown(options['file'].read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError, ValueError) as error:
            raise CommandError(str(error)) from error
        # Powtórzenia tego samego autora i naboru w pliku łączymy, nie mnożymy rekordów.
        grouped = {}
        for record in records:
            key = (normalize_key(record['email']), normalize_key(record['recruitment']))
            if key not in grouped:
                grouped[key] = record.copy()
                continue
            target = grouped[key]
            for field in ('full_name', 'phone_number'):
                if target[field] and record[field] and normalize_key(target[field]) != normalize_key(record[field]):
                    raise CommandError(f'Wiersze {target["line"]} i {record["line"]}: sprzeczne dane kontaktowe.')
                target[field] = target[field] or record[field]
            for field in ('title', 'submission_dates', 'accepted_titles', 'rejected_titles'):
                target[field] = '\n'.join(split_list(target[field] + '\n' + record[field]))
        counts = Counter()
        campaigns = Counter()
        try:
            with transaction.atomic():
                for record in grouped.values():
                    line = record['line']
                    candidates = list(Author.objects.select_for_update().filter(email__iexact=record['email']).order_by('pk')[:2])
                    if len(candidates) > 1:
                        raise CommandError(f'Wiersz {line}: kilka kont autora ma ten sam e-mail. Najpierw rozstrzygnij powiązanie.')
                    if candidates:
                        author = candidates[0]
                    else:
                        first_name, last_name = author_names(record['full_name'])
                        author = Author(first_name=first_name, last_name=last_name, email=record['email'])
                        author.full_clean()
                        author.save()
                        counts['authors_created'] += 1
                    existing = list(Extract.objects.select_for_update().filter(author=author, recruitment__iexact=record['recruitment'])[:2])
                    if len(existing) > 1:
                        raise CommandError(f'Wiersz {line}: powtórzony udział autora; uruchom migracje przed importem.')
                    item = existing[0] if existing else Extract(author=author)
                    before = {field: getattr(item, field) for field in FIELDS} if existing else None
                    for field in FIELDS:
                        setattr(item, field, record[field])
                    item.full_clean()
                    after = {field: getattr(item, field) for field in FIELDS}
                    if before == after:
                        counts['unchanged'] += 1
                    elif existing and not options['update_existing']:
                        raise CommandError(f'Wiersz {line}: dane różnią się od istniejącego udziału #{item.pk}. Nic nie zapisano. Zweryfikuj różnice; świadome zastąpienie wymaga --update-existing.')
                    else:
                        item.save()
                        counts['updated' if existing else 'created'] += 1
                    campaigns[item.recruitment] += 1
                if not options['apply']:
                    transaction.set_rollback(True)
        except (ValidationError, ValueError, IntegrityError) as error:
            if isinstance(error, IntegrityError):
                detail = 'Konflikt unikalności lub równoległy zapis. Sprawdź dane i ponów import.'
            else:
                detail = '; '.join(error.messages) if isinstance(error, ValidationError) else str(error)
            raise CommandError(f'Wiersz {line}: {detail} Cały import wycofano.') from error
        self.stdout.write('ZAPISANO' if options['apply'] else 'PRÓBA — nic nie zapisano')
        self.stdout.write(f'Wiersze pliku: {len(records)}; udziały autor–antologia: {len(grouped)}')
        self.stdout.write(f'Nowe udziały: {counts["created"]}; zmienione: {counts["updated"]}; bez zmian: {counts["unchanged"]}; nowi autorzy: {counts["authors_created"]}')
        for name, count in sorted(campaigns.items()):
            self.stdout.write(f'{name}: {count}')
