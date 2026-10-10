"""Import zgłoszonych uwag do wydanych antologii (Uwagi do antologii).

Domyślnie tylko podgląd; zapis wymaga --apply. Ponowny import tego samego pliku
nie tworzy duplikatów (klucz zapisu wynika z treści wiersza).
"""
import hashlib
import json
import unicodedata
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import AnthologyCorrection
from people.models import Person
from texts.models import Anthology, Text

SCHEMA = 'anthology-corrections-v1'
FIELDS = ('anthology', 'reporter', 'story', 'fragment', 'problem', 'suggestion')
OTHER_PLACE = 'Inne miejsce'


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def row_key(row):
    payload = json.dumps([row[name].strip() for name in FIELDS], ensure_ascii=False)
    return uuid.uuid5(uuid.NAMESPACE_URL, 'fantazmaty/anthology-corrections/v1/' + payload)


class Command(BaseCommand):
    help = 'Uwagi do antologii z pliku JSON. Domyślnie podgląd; zapis: --apply.'

    def add_arguments(self, parser):
        parser.add_argument('input')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--map', dest='mapping',
                            help='JSON: anthologies {tytuł: ID antologii}, users {zgłaszający: ID konta}.')

    def handle(self, *args, **options):
        try:
            raw = Path(options['input']).read_bytes()
            payload = json.loads(raw.decode('utf-8-sig'))
            mapping = json.loads(Path(options['mapping']).read_text(encoding='utf-8-sig')) if options['mapping'] else {}
            rows = payload.get('corrections') if isinstance(payload, dict) else None
            if payload.get('schema') != SCHEMA or not isinstance(rows, list) or not rows:
                raise ValueError(f'Niewłaściwy format danych (oczekiwano schema={SCHEMA}).')
            if not isinstance(mapping, dict) or any(not isinstance(mapping.get(k, {}), dict) for k in ('anthologies', 'users')):
                raise ValueError('Nieprawidłowy format mapowania.')
            for number, row in enumerate(rows, 1):
                if not isinstance(row, dict) or any(not isinstance(row.get(name, ''), str) for name in FIELDS):
                    raise ValueError(f'Wiersz {number}: wszystkie pola muszą być tekstem.')
                for name in FIELDS:
                    row.setdefault(name, '')
                if not row['anthology'].strip() or not row['reporter'].strip():
                    raise ValueError(f'Wiersz {number}: brak antologii lub zgłaszającego.')
                if not (row['fragment'].strip() or row['problem'].strip() or row['suggestion'].strip()):
                    raise ValueError(f'Wiersz {number}: pusta uwaga.')
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise CommandError(str(exc)) from exc

        report = {'mode': 'zapis' if options['apply'] else 'podgląd – nic nie zapisano',
                  'input_sha256': hashlib.sha256(raw).hexdigest(),
                  'created': 0, 'unchanged': 0, 'errors': [], 'notes': []}
        books = list(Anthology.objects.order_by('pk'))
        users = list(get_user_model().objects.filter(is_active=True).order_by('pk'))
        people = list(Person.objects.exclude(user__isnull=True))
        plan, seen = [], set()
        for number, row in enumerate(rows, 1):
            title = row['anthology'].strip()
            book_id = mapping.get('anthologies', {}).get(title)
            matches = [b for b in books if (b.pk == book_id if book_id is not None else normalized(b.title) == normalized(title))]
            if len(matches) != 1:
                report['errors'].append(f'Wiersz {number}: antologia „{title}” – znaleziono {len(matches)} rekordów. Użyj --map.')
                continue
            book = matches[0]
            if book.status != Anthology.Status.READY:
                report['errors'].append(f'Wiersz {number}: antologia „{book.title}” nie jest wydana – uwaga nie byłaby widoczna na liście.')
                continue
            story = row['story'].strip() or OTHER_PLACE
            texts = [t for t in Text.objects.filter(anthology=book) if normalized(t.title) == normalized(story)]
            text = texts[0] if len(texts) == 1 else None
            if text is None and normalized(story) != normalized(OTHER_PLACE):
                report['notes'].append(f'Wiersz {number}: „{story}” nie jest tytułem opowiadania w „{book.title}” – zapisano jako miejsce.')
            reporter = ' '.join(row['reporter'].split())
            user_id = mapping.get('users', {}).get(reporter)
            if user_id is not None:
                found = [u for u in users if u.pk == user_id]
            else:
                ids = {p.user_id for p in people if normalized(f'{p.first_name} {p.last_name}') == normalized(reporter)}
                found = [u for u in users if u.pk in ids or normalized(u.get_full_name()) == normalized(reporter)]
            user = found[0] if len(found) == 1 else None
            if user is None:
                report['notes'].append(f'Wiersz {number}: zgłaszający „{reporter}” bez jednoznacznego konta – zapisano samo imię i nazwisko.')
            key = row_key(row)
            if key in seen:
                report['notes'].append(f'Wiersz {number}: powtórzony wiersz – pominięto.')
                continue
            seen.add(key)
            plan.append(AnthologyCorrection(
                anthology=book, text=text, story_title=text.title if text else story,
                fragment=row['fragment'].strip(), problem=row['problem'].strip(), suggestion=row['suggestion'].strip(),
                submitted_by=user, reporter_name=reporter, submission_key=key))
        if report['errors']:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            raise CommandError('Popraw błędy (np. --map) – nic nie zapisano.')
        with transaction.atomic():
            existing = set(AnthologyCorrection.objects.filter(
                submission_key__in=[item.submission_key for item in plan]).values_list('submission_key', flat=True))
            for item in plan:
                if item.submission_key in existing:
                    report['unchanged'] += 1
                    continue
                item.full_clean(exclude=['submitted_by'])
                if options['apply']:
                    item.save()
                report['created'] += 1
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
