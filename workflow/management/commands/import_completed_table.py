"""Import prepared historical table: existing anthologies, unknown dates, no login grants."""
import hashlib
import html
import json
import unicodedata
import uuid
from collections import Counter
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from authors.models import Author
from people.models import Person
from texts.models import Anthology, Text
from workflow.catalog import all_stage_roles
from workflow.completed_import import import_completed_workflow


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


def identity_key(obj):
    return normalized(obj.first_name), normalized(obj.last_name)


class Command(BaseCommand):
    help = 'Import gotowych tekstów z przygotowanego JSON. Domyślnie podgląd; zapis z --apply.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_importu_archiwum')
        parser.add_argument('--html-report', action='store_true', help='Opcjonalnie zapisz również raport HTML.')
        parser.add_argument('--anthology', action='append', help='Importuj tylko wskazaną antologię; opcję można powtórzyć.')

    def validate(self, data):
        if not isinstance(data, dict) or set(data) != {'schema_version', 'source', 'texts'} or data['schema_version'] != 1:
            raise ValueError('Wymagane schema_version=1, source, texts.')
        if not isinstance(data['source'], str) or not data['source'].strip() or len(data['source']) > 100:
            raise ValueError('Nieprawidłowe źródło importu.')
        if not isinstance(data['texts'], list) or not data['texts']:
            raise ValueError('Brak tekstów do importu.')
        seen_rows, seen_titles = set(), set()
        for row in data['texts']:
            if not isinstance(row, dict) or set(row) != {'source_row', 'title', 'anthology', 'length', 'length_source', 'authors', 'stages'}:
                raise ValueError('Nieprawidłowe pola tekstu.')
            if type(row['source_row']) is not int or row['source_row'] < 1 or row['source_row'] in seen_rows:
                raise ValueError('Powtórzony albo nieprawidłowy numer wiersza.')
            seen_rows.add(row['source_row'])
            if any(not isinstance(row[k], str) or not row[k].strip() for k in ('title', 'anthology')):
                raise ValueError('Tytuł i antologia są wymagane.')
            key = normalized(row['anthology']), normalized(row['title'])
            if key in seen_titles:
                raise ValueError('Powtórzona para antologia–tytuł.')
            seen_titles.add(key)
            if type(row['length']) is not int or row['length'] < 1:
                raise ValueError(f"{row['title']}: brak policzonej długości w znakach ze spacjami.")
            if not isinstance(row['length_source'], dict) or not row['length_source'].get('sha256'):
                raise ValueError('Brak pochodzenia pomiaru długości.')
            if not isinstance(row['authors'], list) or not row['authors'] or not isinstance(row['stages'], list) or not row['stages']:
                raise ValueError('Wymagana lista autorów i zakończonych prac.')
            for name in row['authors']:
                self.validate_name(name, 'author_id')
            for stage in row['stages']:
                if not isinstance(stage, dict) or set(stage) != {'stage_type', 'person', 'source_column'}:
                    raise ValueError('Nieprawidłowe dane etapu.')
                if stage['stage_type'] not in all_stage_roles() or not isinstance(stage['source_column'], str):
                    raise ValueError('Nieznany rodzaj pracy.')
                self.validate_name(stage['person'], 'person_id')

    def validate_name(self, name, id_field):
        if not isinstance(name, dict) or set(name) - {'first_name', 'last_name', id_field}:
            raise ValueError('Nieprawidłowe dane osoby.')
        if any(not isinstance(name.get(k), str) or not name[k].strip() for k in ('first_name', 'last_name')):
            raise ValueError('Wymagane imię i nazwisko.')
        if id_field in name and (type(name[id_field]) is not int or name[id_field] < 1):
            raise ValueError('Identyfikator musi być dodatnią liczbą całkowitą.')

    def resolve(self, model, name, id_field):
        records = model.objects.select_for_update().all()
        if id_field in name:
            obj = records.filter(pk=name[id_field]).first()
            if not obj:
                raise ValueError(f'Nie istnieje {id_field}={name[id_field]}.')
            return obj
        key = normalized(name['first_name']), normalized(name['last_name'])
        matches = [obj for obj in records if identity_key(obj) == key]
        if len(matches) > 1:
            raise ValueError(f"Niejednoznaczna osoba {name['first_name']} {name['last_name']}; podaj {id_field}. ID: {[x.pk for x in matches]}")
        if not matches:
            variants = [obj for obj in records if normalized(obj.last_name) == key[1]]
            if variants:
                warning = {'name': name, 'kind': model._meta.label,
                           'message': 'Nie znaleziono dokładnego imienia i nazwiska. Istnieją osoby o tym samym nazwisku; nie połączono ich automatycznie.',
                           'candidates': [{'id': obj.pk, 'first_name': obj.first_name, 'last_name': obj.last_name} for obj in variants]}
                if warning not in self.warnings:
                    self.warnings.append(warning)
        return matches[0] if matches else None

    def author(self, name):
        author = self.resolve(Author, name, 'author_id')
        if author is None:
            author = Author(first_name=name['first_name'].strip(), last_name=name['last_name'].strip(), email=None)
            author.full_clean(exclude=['email'])
            author.save()
            self.events.append({'action': 'nowy autor', 'name': str(author), 'email': None})
        return author

    def person(self, name):
        person = self.resolve(Person, name, 'person_id')
        User = get_user_model()
        if person and person.user_id:
            return person.user
        identity = person if person else type('Identity', (), name)()
        matches = [u for u in User.objects.select_for_update().all() if identity_key(u) == identity_key(identity)]
        if len(matches) > 1:
            raise ValueError(f'Niejednoznaczne konto dla {identity.first_name} {identity.last_name}. Powiąż właściwe konto z profilem przed importem.')
        user = matches[0] if matches else None
        if user and Person.objects.filter(user=user).exclude(pk=person.pk if person else None).exists():
            raise ValueError(f'Konto {user.pk} jest powiązane z innym profilem. Wskaż person_id.')
        if user is None:
            user = User(username='archive-' + uuid.uuid4().hex, first_name=identity.first_name.strip(),
                        last_name=identity.last_name.strip(), email='', is_active=False, is_staff=False, is_superuser=False)
            user.set_unusable_password()
            user.full_clean()
            user.save()
            self.events.append({'action': 'nowe nieaktywne konto bez hasła', 'name': user.get_full_name(), 'email': ''})
        if person is None:
            person = Person(first_name=user.first_name, last_name=user.last_name, email=None,
                            user=user, is_active=user.is_active)
            person.full_clean(exclude=['email'])
            person.save()
            self.events.append({'action': 'nowy profil', 'name': str(person), 'email': None})
        else:
            person.user = user
            person.full_clean(exclude=['email'] if not person.email else [])
            person.save(update_fields=['user'])
            self.events.append({'action': 'powiązanie profilu z kontem', 'name': str(person)})
        return user

    def import_row(self, row, source, anthology):
        before = len(self.events)
        authors = [self.author(name) for name in row['authors']]
        if len({a.pk for a in authors}) != len(authors):
            raise ValueError('Ten sam autor występuje kilka razy przy tekście.')
        stages = [{'stage_type': s['stage_type'], 'assigned_to_id': self.person(s['person']).pk} for s in row['stages']]
        text = Text.objects.select_for_update().filter(import_source=source, import_source_row=row['source_row']).first()
        if text:
            if (text.title != row['title'].strip() or text.anthology_id != anthology.pk or text.length != row['length']
                    or set(text.authors.values_list('pk', flat=True)) != {a.pk for a in authors}):
                raise ValueError('Istniejący tekst różni się od danych importu; nie nadpisano go.')
        else:
            # Attach a ready anthology only after a terminal workflow exists.
            text = Text(title=row['title'].strip(), length=row['length'], import_source=source, import_source_row=row['source_row'])
            text.full_clean()
            text.save()
            text.authors.set(authors)
        changed = import_completed_workflow(text_id=text.pk, stages=stages, next_stage='ready', preserve_executions=True)
        if text.anthology_id is None:
            text.anthology = anthology
            text.full_clean()
            text.save(update_fields=['anthology'])
        return {'row': row['source_row'], 'title': text.title, 'anthology': anthology.title,
                'length': text.length, 'completed_executions': len(stages),
                'result': 'dodanie' if changed else 'bez zmian', 'events': self.events[before:]}

    def handle(self, *args, **options):
        self.events = []
        self.warnings = []
        results, conflicts = [], []
        try:
            payload = Path(options['file']).read_bytes()
            data = json.loads(payload.decode('utf-8-sig'))
            self.validate(data)
            if options['anthology']:
                selected = {normalized(title) for title in options['anthology']}
                available = {normalized(row['anthology']) for row in data['texts']}
                if selected - available:
                    raise ValueError('Wybrana antologia nie występuje w pliku importu.')
                data['texts'] = [row for row in data['texts'] if normalized(row['anthology']) in selected]
            with transaction.atomic():
                anthologies = {}
                for title in sorted({row['anthology'] for row in data['texts']}):
                    matches = [a for a in Anthology.objects.select_for_update().order_by('pk') if normalized(a.title) == normalized(title)]
                    if len(matches) != 1:
                        raise ValueError(f'Antologia „{title}” musi wskazywać dokładnie jeden istniejący rekord.')
                    anthology = matches[0]
                    expected_rows = [r['source_row'] for r in data['texts'] if r['anthology'] == title]
                    unexpected = Text.objects.filter(anthology=anthology).exclude(import_source=data['source'], import_source_row__in=expected_rows)
                    if unexpected.exists():
                        raise ValueError(f'Antologia „{title}” nie jest pusta; zawiera teksty spoza tego importu.')
                    anthologies[title] = anthology
                for row in data['texts']:
                    before = len(self.events)
                    try:
                        with transaction.atomic():
                            results.append(self.import_row(row, data['source'], anthologies[row['anthology']]))
                    except (ValueError, ValidationError, IntegrityError) as exc:
                        del self.events[before:]
                        conflicts.append({'row': row['source_row'], 'title': row['title'], 'error': str(exc)})
                if conflicts or not options['apply']:
                    transaction.set_rollback(True)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, ValidationError, IntegrityError) as exc:
            conflicts.append({'error': str(exc)})
        mode = 'wycofano – nic nie zapisano' if conflicts else 'zapisano' if options['apply'] else 'podgląd – nic nie zapisano'
        report = {'mode': mode, 'input_sha256': hashlib.sha256(payload).hexdigest() if 'payload' in locals() else None,
                  'counts': dict(Counter(event['action'] for event in self.events)), 'texts': results, 'conflicts': conflicts, 'warnings': self.warnings,
                  'dates': 'Nieznane daty rozpoczęcia, zakończenia i przydziału pozostają puste.',
                  'identities': 'Dopasowanie imienia i nazwiska lub jawnego ID; bez zgadywania pseudonimów i dawnych nazwisk. Nowe konta nieaktywne, bez hasła. Nowe profile bez nadawania ról; istniejące profile zachowują swoje role.'}
        target = Path(options['report'])
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        target.with_suffix('.json').write_text(encoded, encoding='utf-8')
        if options['html_report']:
            target.with_suffix('.html').write_text('<!doctype html><html lang="pl"><meta charset="utf-8"><title>Import archiwum</title><style>body{max-width:1100px;margin:32px auto;padding:0 20px;font:16px/1.5 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Import zakończonych prac</h1><p>'+html.escape(mode)+'</p><pre>'+html.escape(encoded)+'</pre></html>', encoding='utf-8')
        self.stdout.write(encoded)
        if conflicts:
            raise CommandError('Import przerwany; nie zapisano żadnych zmian. Szczegóły w raporcie.')
