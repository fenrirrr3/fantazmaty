"""Import explicitly credited historical post-layout proofreading, without invented dates."""
import hashlib
import json
import unicodedata
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from people.models import Person
from texts.models import Anthology
from core.models import PostLayoutAssignment


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


class Command(BaseCommand):
    help = 'Historyczna korekta poskładowa ze stopek. Domyślnie podgląd; zapis: --apply.'

    def add_arguments(self, parser):
        parser.add_argument('input')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_poskladowa.json')
        parser.add_argument('--map', dest='mapping', help='JSON: anthologies {tytuł: ID}, users {nazwisko: ID konta}.')

    def handle(self, *args, **options):
        try:
            raw = Path(options['input']).read_bytes()
            payload = json.loads(raw.decode('utf-8-sig'))
            mapping = json.loads(Path(options['mapping']).read_text(encoding='utf-8-sig')) if options['mapping'] else {}
            if payload.get('schema') != 'post-layout-credits-v1' or not isinstance(payload.get('credits'), list) or not payload['credits']:
                raise ValueError('Niewłaściwy format danych.')
            if not isinstance(mapping, dict) or any(not isinstance(mapping.get(k, {}), dict) for k in ('anthologies', 'users')):
                raise ValueError('Nieprawidłowy format mapowania.')
            report_path = Path(options['report'])
            if report_path.resolve() == Path(options['input']).resolve() or (options['mapping'] and report_path.resolve() == Path(options['mapping']).resolve()):
                raise ValueError('Raport nie może nadpisać pliku danych lub mapowania.')
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise CommandError(str(exc)) from exc
        report = {'mode': 'podgląd – nic nie zapisano', 'input_sha256': hashlib.sha256(raw).hexdigest(),
            'created': 0, 'unchanged': 0, 'credits': [], 'conflicts': [],
            'notes': 'Zakończone prace historyczne. Strony, daty pracy i osoba przydzielająca pozostają puste. Bez zmian kont i ról.'}
        with transaction.atomic():
            books = list(Anthology.objects.select_for_update().order_by('pk'))
            users = list(get_user_model().objects.order_by('pk'))
            people = list(Person.objects.all())
            seen = set()
            plan = []
            for row in payload['credits']:
                try:
                    if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ('anthology', 'proofreader')):
                        raise ValueError('Nieprawidłowy wiersz danych.')
                    title, name = row['anthology'], row['proofreader']
                    book_id = mapping.get('anthologies', {}).get(title)
                    matches = [b for b in books if (b.pk == book_id if book_id is not None else normalized(b.title) == normalized(title))]
                    if len(matches) != 1 or matches[0].is_novel:
                        raise ValueError(f'Antologia „{title}”: wymagany dokładnie jeden istniejący rekord antologii. W razie innej nazwy użyj --map.')
                    book = matches[0]
                    user_id = mapping.get('users', {}).get(name)
                    if user_id is not None:
                        candidates = [u for u in users if u.pk == user_id]
                    else:
                        profiles = [p for p in people if normalized(f'{p.first_name} {p.last_name}') == normalized(name)]
                        if any(not p.user_id for p in profiles):
                            raise ValueError(f'„{name}”: istnieje profil bez konta; wymagane jawne powiązanie, nie tworzę drugiego profilu.')
                        ids = {p.user_id for p in profiles}
                        candidates = [u for u in users if u.pk in ids or normalized(u.get_full_name()) == normalized(name)]
                    if len(candidates) != 1:
                        raise ValueError(f'„{name}”: znaleziono {len(candidates)} kont. Użyj users w --map (ID konta), bez zgadywania nazwisk.')
                    user = candidates[0]
                    pair = (book.pk, user.pk)
                    if pair in seen:
                        raise ValueError(f'Powtórzenie w danych: {title} / {name}.')
                    seen.add(pair)
                    key = uuid.uuid5(uuid.NAMESPACE_URL, f'fantazmaty/post-layout-credits/v1/{book.pk}/{user.pk}')
                    existing = PostLayoutAssignment.objects.select_for_update().filter(creation_key=key).first()
                    if existing:
                        if (existing.anthology_id, existing.proofreader_id, existing.historical, existing.status) != (book.pk, user.pk, True, 'completed'):
                            raise ValueError(f'Wcześniejszy import został zmieniony: {title} / {name}. Wymaga sprawdzenia.')
                        report['unchanged'] += 1
                        action = 'bez zmian'
                    elif PostLayoutAssignment.objects.filter(anthology=book, proofreader=user).exists():
                        raise ValueError(f'Istnieje już korekta tej osoby dla antologii: {title} / {name}. Nie dubluję ani nie nadpisuję istniejącej pracy.')
                    else:
                        item = PostLayoutAssignment(anthology=book, proofreader=user, status='completed', historical=True,
                            page_from=None, page_to=None, assigned_start=None, created_by=None, creation_key=key)
                        item.full_clean()
                        plan.append(item)
                        action = 'dodanie'
                    report['credits'].append({'anthology': book.title, 'anthology_id': book.pk, 'proofreader': name, 'user_id': user.pk, 'action': action})
                except (ValueError, TypeError, ValidationError) as exc:
                    report['conflicts'].append(str(exc))
            if report['conflicts']:
                report['mode'] = 'przerwano – nic nie zapisano'
            else:
                for item in plan:
                    item.save()
                report['created'] = len(plan)
                if options['apply']:
                    report['mode'] = 'zapisano'
            # Write while still inside the transaction: a failed report write rolls back the import.
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if report['conflicts'] or not options['apply']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if report['conflicts']:
            raise CommandError(f'Nie zapisano zmian. Szczegóły: {report_path}')
