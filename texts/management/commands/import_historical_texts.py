"""Atomic, repeatable import of the reviewed archive; no users or live assignments."""
import json
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from authors.models import Author
from people.models import Person
from texts.models import Anthology, HistoricalTextAssignment, Text
from workflow.models import WorkflowStage


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').split()).casefold()


class Command(BaseCommand):
    help = 'Import tekstów historycznych i osób z JSON; domyślnie podgląd, zapis z --commit.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        try:
            data = json.loads(Path(options['file']).read_text(encoding='utf-8-sig'))
            if set(data) != {'schema_version', 'texts'} or data['schema_version'] != 1 or not isinstance(data['texts'], list) or not data['texts']:
                raise ValueError('Nieprawidłowy format dokumentu (schema_version=1, texts).')
            self.counts = Counter()
            self.cache = {}
            self.names = {}
            for model in (Author, Person):
                index = defaultdict(list)
                for obj in model.objects.all():
                    index[(key(obj.first_name), key(obj.last_name))].append(obj)
                self.names[model] = index
            self.anthologies = defaultdict(list)
            for anthology in Anthology.objects.all():
                self.anthologies[key(anthology.title)].append(anthology)
            seen = set()
            with transaction.atomic():
                for row in data['texts']:
                    self.current_row = row.get('historical_source_row', '?')
                    identity = (row.get('historical_source'), row.get('historical_source_row'))
                    if identity in seen:
                        raise ValueError('Powtórzone LP w źródle.')
                    seen.add(identity)
                    self.import_text(row)
                if not options['commit']:
                    transaction.set_rollback(True)
            prefix = 'ZAPISANO' if options['commit'] else 'PODGLĄD — bez zapisu'
            self.stdout.write(self.style.SUCCESS(prefix))
            self.stdout.write(json.dumps(dict(self.counts), ensure_ascii=False, sort_keys=True))
        except (OSError, ValueError, TypeError, KeyError, ValidationError, IntegrityError, Author.DoesNotExist, Person.DoesNotExist) as exc:
            raise CommandError(f'LP {getattr(self, "current_row", "?")}: {exc}. Cały import wycofano.') from exc

    def resolve_person(self, model, data):
        allowed = {'id', 'first_name', 'last_name', 'email'} | ({'is_blacklisted', 'contact'} if model is Author else set())
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError('Nieznane pola osoby/autora.')
        for field in ('first_name', 'last_name'):
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise ValueError(f'Brak {field} osoby.')
        name = (key(data['first_name']), key(data['last_name']))
        cache_key = (model, data.get('id'), *name)
        if cache_key in self.cache:
            obj = self.cache[cache_key]
        else:
            if data.get('id') is not None:
                if type(data['id']) is not int or data['id'] < 1:
                    raise ValueError('Nieprawidłowe id osoby.')
                obj = model.objects.get(pk=data['id'])
            else:
                matches = self.names[model].get(name, [])
                if len(matches) > 1:
                    raise ValueError(f'Niejednoznaczna osoba {data["first_name"]} {data["last_name"]}; ID: {[x.pk for x in matches]}. Wskaż id w danych.')
                obj = matches[0] if matches else None
                if obj is None:
                    email = data.get('email') or None
                    if email and model.objects.filter(email__iexact=email).exists():
                        raise ValueError('E-mail istnieje przy innej osobie; wskaż id zamiast tworzyć duplikat.')
                    obj = model(first_name=data['first_name'], last_name=data['last_name'], email=email)
                    if model is Person:
                        obj.is_active = False
                        obj.previous_data = 'Profil historyczny utworzony podczas importu archiwum. Aktualna aktywność i dane kontaktowe wymagają uzupełnienia.'
                    else:
                        obj.contact = bool(email)
                    obj.full_clean(exclude=['email'] if email is None else [])
                    obj.save()
                    self.names[model][name].append(obj)
                    self.counts['nowi_autorzy' if model is Author else 'nowe_osoby'] += 1
                else:
                    self.counts['dopasowani_autorzy' if model is Author else 'dopasowane_osoby'] += 1
            self.cache[cache_key] = obj
        # Explicit source restrictions only; never erase known email or reactivate anyone.
        if model is Author:
            updates = []
            for field in ('is_blacklisted', 'contact'):
                if field in data:
                    if type(data[field]) is not bool:
                        raise ValueError(f'{field} wymaga booleana.')
                    if getattr(obj, field) != data[field]:
                        setattr(obj, field, data[field]); updates.append(field)
            if updates:
                obj.save(update_fields=updates)
                self.counts['aktualizacje_autorow'] += 1
        return obj

    def import_text(self, row):
        expected = {'historical_source', 'historical_source_row', 'title', 'anthology', 'authors', 'length', 'is_historical', 'coordinator_note', 'historical_assignments', 'workflow_stages'}
        if set(row) != expected or row['is_historical'] is not True:
            raise ValueError('Nieprawidłowe pola tekstu lub brak is_historical=true.')
        if not isinstance(row['historical_source'], str) or not row['historical_source'].strip():
            raise ValueError('Brak źródła importu.')
        if type(row['historical_source_row']) is not int or row['historical_source_row'] < 1:
            raise ValueError('LP musi być dodatnią liczbą całkowitą.')
        if type(row['length']) is not int or row['length'] < 1:
            raise ValueError('Długość musi być dodatnią liczbą całkowitą.')
        if not isinstance(row['title'], str) or not row['title'].strip():
            raise ValueError('Brak tytułu.')
        stages = row['workflow_stages']
        if not isinstance(stages, list) or len(stages) != 1:
            raise ValueError('Wymagany jeden terminalny etap.')
        stage = stages[0]
        if set(stage) != {'stage_type', 'started_at', 'ended_at', 'is_completed', 'workflow_cycle', 'iteration'} or stage['stage_type'] not in ('ready', 'withdrawn') or stage['started_at'] is not None or stage['ended_at'] is not None or stage['is_completed'] is not False or stage['workflow_cycle'] != 1 or stage['iteration'] != 1:
            raise ValueError('Dozwolony wyłącznie terminalny etap ready/withdrawn bez dat, w cyklu 1.')
        if not isinstance(row['authors'], list) or not row['authors']:
            raise ValueError('Tekst musi mieć autorów.')
        if set(row['anthology']) != {'title'} or not isinstance(row['anthology']['title'], str) or not row['anthology']['title'].strip():
            raise ValueError('Nieprawidłowa antologia.')
        matches = self.anthologies.get(key(row['anthology']['title']), [])
        if len(matches) > 1:
            raise ValueError('Niejednoznaczna antologia; uporządkuj duplikaty przed importem.')
        if matches:
            anthology = matches[0]
        else:
            anthology = Anthology(title=row['anthology']['title'])
            anthology.full_clean(); anthology.save()
            self.anthologies[key(anthology.title)].append(anthology)
            self.counts['nowe_antologie'] += 1
        authors = [self.resolve_person(Author, a) for a in row['authors']]
        identity = dict(historical_source=row['historical_source'], historical_source_row=row['historical_source_row'])
        text = Text.objects.select_for_update().filter(**identity).first()
        fields = dict(title=row['title'], anthology=anthology, length=row['length'], is_historical=True, coordinator_note=row['coordinator_note'])
        if text is None:
            if any(key(title) == key(row['title']) for title in Text.objects.filter(anthology=anthology).values_list('title', flat=True)):
                raise ValueError('Tekst o tym tytule i antologii już istnieje bez tego identyfikatora źródła. Nie utworzono duplikatu.')
            text = Text(**identity, **fields)
            text.full_clean(); text.save(); text.authors.set(authors)
            terminal = WorkflowStage(text=text, **stage)
            terminal.full_clean(); terminal.save()
            self.counts['nowe_teksty'] += 1
        else:
            if any(getattr(text, field) != value for field, value in fields.items()) or set(text.authors.values_list('pk', flat=True)) != {a.pk for a in authors}:
                raise ValueError('Istniejący tekst różni się od danych importu; nie nadpisano zmian.')
            terminal = list(text.workflow_stages.all())
            if text.current_workflow_cycle != 1 or len(terminal) != 1 or any(getattr(terminal[0], f) != v for f, v in stage.items()):
                raise ValueError('Istniejący workflow zmienił się od importu; nie nadpisano go.')
            self.counts['istniejace_teksty'] += 1
        expected_keys = set()
        if not isinstance(row['historical_assignments'], list):
            raise ValueError('historical_assignments musi być listą.')
        for entry in row['historical_assignments']:
            self.import_assignment(text, entry, row, expected_keys)
        if set(text.historical_assignments.values_list('role', 'position', 'participant')) != expected_keys:
            raise ValueError('Zestaw zapisanych udziałów różni się od importu; niczego nie usunięto.')

    def import_assignment(self, text, entry, row, expected_keys):
        expected = {'person', 'person_name', 'role', 'position', 'participant', 'source_row', 'source_status', 'is_completed', 'started_at', 'ended_at', 'notes'}
        if set(entry) != expected:
            raise ValueError('Nieprawidłowe pola historycznego udziału.')
        if entry['source_row'] != row['historical_source_row'] or entry['source_status'] != row['workflow_stages'][0]['stage_type']:
            raise ValueError('LP lub status udziału nie zgadza się z tekstem.')
        if type(entry['is_completed']) is not bool:
            raise ValueError('is_completed musi być booleanem.')
        for field in ('position', 'participant'):
            if type(entry[field]) is not int or entry[field] < 1:
                raise ValueError('Numer roli i uczestnika muszą być dodatnie.')
        if entry['started_at'] is not None or entry['ended_at'] is not None:
            raise ValueError('Daty historyczne muszą pozostać puste.')
        person = self.resolve_person(Person, entry['person'])
        values = {k:v for k,v in entry.items() if k != 'person'}
        identity = (entry['role'], entry['position'], entry['participant'])
        if identity in expected_keys:
            raise ValueError('Powtórzone przypisanie roli i numeru osoby.')
        expected_keys.add(identity)
        existing = text.historical_assignments.filter(role=identity[0], position=identity[1], participant=identity[2]).first()
        if existing:
            if existing.person_id != person.pk or any(getattr(existing,k) != v for k,v in values.items()):
                raise ValueError('Historyczny udział różni się od zapisanej wersji; nie nadpisano go.')
            self.counts['istniejace_udzialy'] += 1
        else:
            item = HistoricalTextAssignment(text=text, person=person, **values)
            item.full_clean(); item.save()
            self.counts['nowe_udzialy'] += 1
