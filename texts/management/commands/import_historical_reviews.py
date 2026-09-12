"""Import historycznych zgłoszeń. Domyślnie cała transakcja jest wycofywana."""
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from authors.models import Author
from people.models import Person
from texts.models import Anthology, Review, ReviewAssignment, Reviewers, ReviewOpinion


def compact(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').split())


def key(value):
    return compact(value).casefold()


def parts(name):
    # Pseudonim jednowyrazowy nie jest uzupełniany fikcyjnym imieniem.
    bits = compact(name).rsplit(' ', 1)
    return tuple(bits) if len(bits) == 2 else ('', bits[0])


def variants(name):
    names = [compact(name)]
    match = re.fullmatch(r'(.+?)\s*\((.+)\)', name)
    if match:
        names.append(compact(match[1]))
        if key(match[2]) != 'pseudonim':
            names.append(compact(match[2]))
    return {key(n) for n in names}


def index_names(objects, pseudonyms=False):
    index = defaultdict(dict)
    for obj in objects:
        names = [f'{obj.first_name} {obj.last_name}', f'{obj.last_name} {obj.first_name}']
        if pseudonyms and obj.pseudonym:
            names.append(obj.pseudonym)
            alias_first, alias_last = parts(obj.pseudonym)
            names.append(f"{alias_last} {alias_first}")
        for name in names:
            if key(name):
                index[key(name)][obj.pk] = obj
    return index


class Command(BaseCommand):
    help = 'Import archiwalnych recenzji; bez --commit wykonuje próbę i wycofuje wszystkie zapisy.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        try:
            data = json.loads(Path(options['file']).read_text(encoding='utf-8'))
            if data.get('schema_version') != 1 or data.get('all_historical') is not True:
                raise ValueError('Nieobsługiwany format lub brak jawnego oznaczenia archiwum.')
            rows = data['reviews']
            if not isinstance(rows, list) or not rows:
                raise ValueError('Brak rekordów.')
            for model, field in [(Review, 'coauthors'), (ReviewAssignment, 'historical_person')]:
                model._meta.get_field(field)
            self.counts = Counter()
            self.row = None
            with transaction.atomic():
                self.authors = index_names(Author.objects.all(), pseudonyms=True)
                self.author_names = index_names(Author.objects.all())
                self.people = index_names(Person.objects.select_related('user').all())
                self.users = index_names(get_user_model().objects.all())
                self.anthologies = defaultdict(list)
                for obj in Anthology.objects.all():
                    self.anthologies[key(obj.title)].append(obj)
                seen = set()
                for row in rows:
                    self.row = row.get('row')
                    self.import_row(row, seen)
                if not options['commit']:
                    transaction.set_rollback(True)
            mode = 'ZAPISANO' if options['commit'] else 'PRÓBA — wszystkie zapisy wycofane'
            self.stdout.write(self.style.SUCCESS(mode))
            for label, counter in [('Zgłoszenia nowe', 'reviews'), ('Zgłoszenia już obecne', 'skipped'),
                                   ('Oceny nowe', 'assignments'), ('Autorzy nowi', 'authors'),
                                   ('Profile historyczne nowe', 'people'), ('Antologie nowe', 'anthologies')]:
                self.stdout.write(f'{label}: {self.counts[counter]}')
        except Exception as exc:
            raise CommandError(f'Import przerwany, transakcja wycofana. Wiersz {getattr(self, "row", "?")}: {exc}') from exc

    def resolve(self, name, *, author):
        index = self.authors if author else self.people
        base_name = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
        # Jawne imię i nazwisko ma pierwszeństwo przed pseudonimami innych rekordów.
        matches = dict(self.author_names.get(key(base_name), {})) if author else {}
        if not matches:
            for alias in variants(name):
                matches.update(index.get(alias, {}))
        if len(matches) > 1:
            raise ValueError(f'Niejednoznaczne dopasowanie {name!r}; ID: {sorted(matches)}. Nie łączę rekordów automatycznie.')
        if matches:
            return next(iter(matches.values()))
        user = None
        if not author:
            users = self.users.get(key(name), {})
            if len(users) > 1:
                raise ValueError(f'Kilka kont o nazwie {name!r}; potrzebne jednoznaczne dopasowanie.')
            if users:
                user = next(iter(users.values()))
                existing = Person.objects.filter(user=user).first()
                if existing:
                    index[key(name)][existing.pk] = existing
                    return existing
        base_name = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
        first, last = parts(base_name)
        if author:
            obj = Author(first_name=first, last_name=last, email=None, contact=False)
            if not first or '(pseudonim)' in name:
                obj.pseudonym = base_name
        else:
            obj = Person(first_name=first, last_name=last, email=None, is_active=False, user=user)
        # Import historyczny zna czasem wyłącznie pseudonim. Zachowujemy brak imienia,
        # zamiast zgadywać dane. Pozostałe pola i ograniczenia są walidowane.
        obj.full_clean(exclude=['email'] + (['first_name'] if not first else []))
        obj.save()
        self.counts['authors' if author else 'people'] += 1
        if author:
            self.author_names[key(f'{obj.first_name} {obj.last_name}')][obj.pk] = obj
        for alias in variants(name) | {key(f'{obj.first_name} {obj.last_name}')}:
            index[alias][obj.pk] = obj
        return obj

    def import_row(self, row, seen):
        required = {'row', 'authors', 'title', 'genre', 'length', 'content_warnings', 'anthology',
                    'created_at', 'status', 'author_notified_at', 'general_notes', 'assignments'}
        if set(row) != required:
            raise ValueError('Nieprawidłowe kolumny danych.')
        if not row['authors'] or not all(isinstance(n, str) and compact(n) for n in row['authors']):
            raise ValueError('Brak nazw autora.')
        if not compact(row['title']) or not compact(row['anthology']):
            raise ValueError('Brak tytułu lub naboru.')
        if row['length'] is not None and (type(row['length']) is not int or row['length'] < 1):
            raise ValueError('Nieprawidłowa długość.')
        if row['status'] not in {'accepted', 'rejected', 'withdrawn'}:
            raise ValueError('Nieprawidłowy status archiwalny.')
        created = date.fromisoformat(row['created_at'])
        notified = date.fromisoformat(row['author_notified_at']) if row['author_notified_at'] else None
        if notified and notified < created:
            raise ValueError('Powiadomienie wcześniejsze od zgłoszenia.')
        authors = list({a.pk: a for a in [self.resolve(n, author=True) for n in row['authors']]}.values())
        anthology_matches = self.anthologies[key(row['anthology'])]
        if len(anthology_matches) > 1:
            raise ValueError('Niejednoznaczna antologia.')
        if anthology_matches:
            anthology = anthology_matches[0]
        else:
            anthology = Anthology(title=compact(row['anthology']))
            anthology.full_clean()
            anthology.save()
            anthology_matches.append(anthology)
            self.counts['anthologies'] += 1
        first, last = parts(row['authors'][0])
        notes = row['general_notes']
        signature = 'Podpis autora / autorów: ' + ', '.join(row['authors'])
        notes = (notes + '\n\n' + signature).strip()
        review = Review(author=authors[0], anthology=anthology, title=row['title'],
                        author_first_name=first, author_last_name=last, genre=row['genre'],
                        length=row['length'], content_warnings=row['content_warnings'],
                        email=authors[0].email or '', old_reviews=True, status=row['status'],
                        author_notified_at=notified, decision_at=None,
                        is_hidden=any(a.is_blacklisted for a in authors))
        review.full_clean(exclude=['author_first_name'] if not first else [])
        identity = (key(review.title), anthology.pk, created, tuple(sorted(a.pk for a in authors)))
        if identity in seen:
            raise ValueError('Dwa nierozróżnialne zgłoszenia w pliku (autorzy, tytuł, nabór i data).')
        seen.add(identity)
        planned = []
        positions, people = set(), set()
        for item in row['assignments']:
            if set(item) != {'name', 'position', 'opinion'}:
                raise ValueError('Nieprawidłowe pola oceny.')
            if type(item['position']) is not int or not 1 <= item['position'] <= 6 or item['position'] in positions:
                raise ValueError('Nieprawidłowe lub powtórzone miejsce recenzenta.')
            if item['opinion'] not in ReviewOpinion.values:
                raise ValueError('Nieprawidłowa opinia.')
            person = self.resolve(item['name'], author=False)
            if person.pk in people:
                raise ValueError('Ten sam recenzent zajmuje dwa miejsca w zgłoszeniu.')
            positions.add(item['position']); people.add(person.pk)
            planned.append((item['position'], person, item['opinion']))
        candidates = list(Review.objects.select_for_update().filter(
            title=review.title, anthology=anthology, created_at=created,
        ).prefetch_related('coauthors', 'assignments'))
        matches = []
        for candidate in candidates:
            ids = {candidate.author_id} | set(candidate.coauthors.values_list('pk', flat=True))
            if ids == {a.pk for a in authors} or (
                key(candidate.author_first_name) == key(review.author_first_name)
                and key(candidate.author_last_name) == key(review.author_last_name)
            ):
                matches.append(candidate)
        if len(matches) > 1:
            raise ValueError('Kilka istniejących zgłoszeń pasuje do importu.')
        if matches:
            existing = matches[0]
            fields = ('author_id', 'author_first_name', 'author_last_name', 'genre', 'length',
                      'content_warnings', 'old_reviews', 'status', 'author_notified_at')
            changed = [f for f in fields if getattr(existing, f) != getattr(review, f)]
            if set(existing.coauthors.values_list('pk', flat=True)) != {a.pk for a in authors[1:]}:
                changed.append('coauthors')
            current = sorted((a.position, a.historical_person_id, a.opinion) for a in existing.assignments.all())
            expected = sorted((pos, person.pk, opinion) for pos, person, opinion in planned)
            if current != expected:
                changed.append('assignments')
            note = Reviewers.objects.filter(review=existing).first()
            if note is None or note.general_notes != notes:
                changed.append('general_notes')
            if changed:
                raise ValueError(f'Zgłoszenie ID {existing.pk} już istnieje, ale różni się w: {", ".join(changed)}. Nie nadpisuję go.')
            self.counts['skipped'] += 1
            return
        review.save()
        # auto_now_add jest właściwe dla nowych zgłoszeń, import przywraca datę źródłową.
        Review.objects.filter(pk=review.pk).update(created_at=created)
        review.coauthors.set(authors[1:])
        Reviewers.objects.create(review=review, general_notes=notes)
        for position, person, opinion in planned:
            assignment = ReviewAssignment(review=review, historical_person=person,
                user_id=person.user_id, position=position, opinion=opinion,
                assigned_at=None, opinion_changed_at=None)
            assignment.full_clean()
            assignment.save()
            self.counts['assignments'] += 1
        self.counts['reviews'] += 1
