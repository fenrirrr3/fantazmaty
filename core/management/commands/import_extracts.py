"""Dopisuje do naboru Ekstraktów przyjęte i odrzucone teksty z pliku TSV.

Domyślnie tylko podgląd: wszystkie zapisy wykonuje w transakcji i na końcu ją
wycofuje, więc kolejne wiersze widzą skutki wcześniejszych (np. nowego autora).
Z --apply zapisuje całość albo nic.
"""
import argparse
import re
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from authors.models import Author
from core.normalization import compact, email as normalize_email
from texts.extract_data import normalize_key, split_list
from texts.models import Extract
from texts.services import matching_blacklist_entries, normalize_author_name

HEADER = ('imię i nazwisko', 'pseudonim', 'e-mail', 'tytuły (rozdzielone |)', 'decyzja')
DECISIONS = {'przyjęte': 'accepted_titles', 'odrzucone': 'rejected_titles'}
OPPOSITE = {'accepted_titles': 'rejected_titles', 'rejected_titles': 'accepted_titles'}


class RowError(Exception):
    pass


def quoted(titles):
    return ', '.join(f'„{title}”' for title in titles)


def parse_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Data musi mieć format RRRR-MM-DD.') from None


def parse_rows(text):
    """Zwraca listę (nr wiersza, dane | None, błąd | None). Puste wiersze pomija."""
    lines = text.splitlines()
    numbered = [(number, line) for number, line in enumerate(lines, 1) if line.strip()]
    if not numbered:
        raise CommandError('Plik jest pusty.')
    header = [normalize_key(cell) for cell in numbered[0][1].split('\t')]
    while header and not header[-1]:
        header.pop()
    if tuple(header) != HEADER:
        raise CommandError('Nieprawidłowy nagłówek. Oczekiwano kolumn rozdzielonych tabulatorem: '
                           + ' | '.join(HEADER) + '.')
    rows = []
    for number, line in numbered[1:]:
        cells = [cell.strip() for cell in line.split('\t')]
        while len(cells) > len(HEADER) and not cells[-1]:
            cells.pop()
        try:
            rows.append((number, parse_row(cells), None))
        except RowError as error:
            rows.append((number, None, str(error)))
    return rows


def parse_row(cells):
    if len(cells) != len(HEADER):
        raise RowError(f'oczekiwano {len(HEADER)} kolumn, jest {len(cells)} (sprawdź tabulatory).')
    name, pseudonym, address, titles, decision = cells
    words = compact(name).split()
    if len(words) < 2:
        raise RowError('podaj imię i nazwisko (co najmniej dwa słowa).')
    address = normalize_email(address)
    try:
        validate_email(address)
    except ValidationError:
        raise RowError(f'nieprawidłowy adres e-mail „{address}”.') from None
    field = DECISIONS.get(normalize_key(decision))
    if field is None:
        raise RowError(f'nieznana decyzja „{decision}” – wpisz „przyjęte” albo „odrzucone”.')
    result, seen = [], set()
    for title in titles.split('|'):
        title = compact(title)
        if ';' in title:
            raise RowError(f'tytuł „{title}” zawiera średnik, który w Ekstraktach rozdziela tytuły.')
        if title and normalize_key(title) not in seen:
            seen.add(normalize_key(title))
            result.append(title)
    if not result:
        raise RowError('brak tytułów.')
    return {'name': ' '.join(words), 'first_name': words[0], 'last_name': ' '.join(words[1:]),
            'pseudonym': compact(pseudonym), 'email': address, 'titles': result, 'field': field}


class Command(BaseCommand):
    help = ('Importuje teksty do naboru Ekstraktów z pliku TSV (imię i nazwisko, pseudonim, e-mail, '
            'tytuły rozdzielone |, decyzja). Bez --apply tylko podgląd.')

    def add_arguments(self, parser):
        parser.add_argument('path', help='Plik TSV w UTF-8.')
        parser.add_argument('--recruitment', required=True, help='Dokładna nazwa naboru z listy Ekstraktów.')
        parser.add_argument('--date', type=parse_date, help='Data nadesłania nowych wpisów (RRRR-MM-DD), domyślnie dziś.')
        parser.add_argument('--apply', action='store_true', help='Zapisz zmiany (domyślnie tylko podgląd).')

    def handle(self, *args, **options):
        try:
            with open(options['path'], encoding='utf-8-sig') as source:
                rows = parse_rows(source.read())
        except (OSError, UnicodeDecodeError) as error:
            raise CommandError(f'Nie można odczytać pliku: {error}') from error
        self.submitted = (options['date'] or timezone.localdate()).isoformat()
        self.counts = {'authors': 0, 'entries': 0, 'added': 0, 'skipped': 0}
        errors = 0
        with transaction.atomic():
            self.recruitment = self.resolve_recruitment(options['recruitment'])
            self.name_index = {}
            for author in Author.objects.only('pk', 'first_name', 'last_name'):
                self.name_index.setdefault(normalize_author_name(str(author)), []).append(author.pk)
            for number, data, error in rows:
                self.stdout.write(f'Wiersz {number}' + (f' – {data["name"]}:' if data else ':'))
                if data:
                    before, self.new_name = dict(self.counts), None
                    try:
                        # Punkt zapisu: błędny wiersz nie zostawia częściowych zmian w podglądzie.
                        with transaction.atomic():
                            lines = self.import_row(data)
                        if self.new_name:
                            self.name_index.setdefault(self.new_name[0], []).append(self.new_name[1])
                    except RowError as row_error:
                        error = str(row_error)
                    except (ValidationError, IntegrityError) as save_error:
                        messages = getattr(save_error, 'messages', None) or [str(save_error)]
                        error = 'zapis odrzucony: ' + ' '.join(messages)
                if error:
                    if data:
                        self.counts = before
                    errors += 1
                    self.stdout.write(f'  BŁĄD: {error}')
                else:
                    for line in lines:
                        self.stdout.write(f'  {line}')
            if errors or not options['apply']:
                transaction.set_rollback(True)
        summary = (f'nowi autorzy: {self.counts["authors"]}, nowe wpisy Ekstraktów: {self.counts["entries"]}, '
                   f'dopisane tytuły: {self.counts["added"]}, pominięte tytuły (już są): {self.counts["skipped"]}')
        if errors:
            raise CommandError(f'Błędne wiersze: {errors}. Nic nie zapisano – popraw plik i uruchom ponownie '
                               f'(plan bez błędnych wierszy: {summary}).')
        if options['apply']:
            self.stdout.write(self.style.SUCCESS(f'Zapisano w naborze „{self.recruitment}” – {summary}.'))
        else:
            self.stdout.write(f'Podgląd – nic nie zapisano. Nabór „{self.recruitment}”: {summary}. '
                              'Aby zapisać, uruchom ponownie z --apply.')

    def resolve_recruitment(self, value):
        """Używa istniejącej pisowni naboru, gdy różni się tylko wielkością liter lub spacjami."""
        key = normalize_key(value)
        if not key:
            raise CommandError('Podaj nazwę naboru.')
        spellings = sorted({name for name in Extract.objects.values_list('recruitment', flat=True).distinct()
                            if normalize_key(name) == key})
        if not spellings:
            self.stdout.write(self.style.WARNING(
                f'Ostrzeżenie: w Ekstraktach nie ma jeszcze naboru „{compact(value)}” – sprawdź, czy to nie literówka.'))
            return compact(value)
        if len(spellings) > 1:
            self.stdout.write(self.style.WARNING('Ostrzeżenie: nabór ma kilka pisowni: ' + quoted(spellings)
                                                 + f' – użyto „{spellings[0]}”.'))
        elif spellings[0] != value:
            self.stdout.write(f'Użyto istniejącej pisowni naboru: „{spellings[0]}”.')
        return spellings[0]

    def find_author(self, data):
        author = Author.objects.select_for_update().filter(email__iexact=data['email']).first()
        if author is not None:
            return author, 'po e-mailu'
        matches = self.name_index.get(normalize_author_name(data['name']), [])
        if len(matches) > 1:
            raise RowError('kilku autorów o tym imieniu i nazwisku (ID: ' + ', '.join(map(str, matches))
                           + ') – wpisz w pliku e-mail autora zapisany w CMS.')
        if matches:
            return Author.objects.select_for_update().get(pk=matches[0]), 'po imieniu i nazwisku'
        return None, ''

    def import_row(self, data):
        lines, warnings = [], []
        author, matched_by = self.find_author(data)
        if author is not None and (author.is_blacklisted or matching_blacklist_entries(author=author, email=data['email']).exists()):
            raise RowError(f'autor #{author.pk} jest na czarnej liście – nie importowano; dodaj wpis ręcznie, jeśli to zamierzone.')
        if author is None and matching_blacklist_entries(email=data['email']).exists():
            raise RowError('adres e-mail jest na czarnej liście – nie importowano; dodaj wpis ręcznie, jeśli to zamierzone.')
        if author is None:
            author = Author(first_name=data['first_name'], last_name=data['last_name'],
                            pseudonym=data['pseudonym'], email=data['email'])
            author.full_clean()
            author.save()
            self.new_name = (normalize_author_name(str(author)), author.pk)
            self.counts['authors'] += 1
            lines.append(f'nowy autor: imię „{author.first_name}”, nazwisko „{author.last_name}”'
                         + (f', pseudonim „{author.pseudonym}”' if author.pseudonym else ''))
        else:
            lines.append(f'istniejący autor #{author.pk} {author} (dopasowany {matched_by})')
            if normalize_author_name(str(author)) != normalize_author_name(data['name']):
                warnings.append(f'imię i nazwisko w pliku („{data["name"]}”) różni się od zapisanego w CMS – dane autora bez zmian.')
            if (author.email or '') != data['email']:
                warnings.append(f'e-mail w pliku ({data["email"]}) różni się od e-maila autora w CMS ({author.email or "brak"}) – dane autora bez zmian.')
            if data['pseudonym'] and normalize_key(data['pseudonym']) != normalize_key(author.pseudonym):
                warnings.append(f'pseudonim w pliku („{data["pseudonym"]}”) różni się od zapisanego w CMS („{author.pseudonym or "brak"}”) – dane autora bez zmian.')
        field, label = data['field'], 'przyjęte' if data['field'] == 'accepted_titles' else 'odrzucone'
        entry = next((item for item in Extract.objects.select_for_update().filter(author=author)
                      if normalize_key(item.recruitment) == normalize_key(self.recruitment)), None)
        if entry is None:
            entry = Extract(author=author, full_name=str(author), email=data['email'], recruitment=self.recruitment,
                            title='\n'.join(data['titles']), submission_dates=self.submitted,
                            **{field: '\n'.join(data['titles'])})
            entry.full_clean()
            entry.save()
            self.counts['entries'] += 1
            self.counts['added'] += len(data['titles'])
            lines.append(f'nowy wpis Ekstraktów, {label}: {quoted(data["titles"])}')
            return lines + [f'ostrzeżenie: {warning}' for warning in warnings]
        submitted = {normalize_key(title) for title in split_list(entry.title)}
        decided = {normalize_key(title) for title in split_list(getattr(entry, field))}
        opposite = {normalize_key(title) for title in split_list(getattr(entry, OPPOSITE[field]))}
        conflicts = [title for title in data['titles'] if normalize_key(title) in opposite]
        if conflicts:
            raise RowError(f'tytuły mają w CMS przeciwną decyzję: {quoted(conflicts)} – popraw plik albo wpis #{entry.pk}.')
        added = [title for title in data['titles'] if normalize_key(title) not in decided]
        skipped = [title for title in data['titles'] if normalize_key(title) in decided]
        if added:
            entry.title = '\n'.join([entry.title] + [title for title in added if normalize_key(title) not in submitted])
            setattr(entry, field, '\n'.join([getattr(entry, field)] + added))
            entry.full_clean()
            entry.save()
            self.counts['added'] += len(added)
            lines.append(f'istniejący wpis Ekstraktów #{entry.pk}, dopisano tytuły ({label}): {quoted(added)}')
        else:
            lines.append(f'istniejący wpis Ekstraktów #{entry.pk} – bez zmian')
        if skipped:
            self.counts['skipped'] += len(skipped)
            lines.append(f'pominięto (już są): {quoted(skipped)}')
        return lines + [f'ostrzeżenie: {warning}' for warning in warnings]
