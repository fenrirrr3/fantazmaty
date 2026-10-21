"""Importuje ostrzeżenia dotyczące treści (Text.content_warnings) z pliku TSV.

Kolumny (pierwsze cztery wymagane, kolejne – np. źródło, uzasadnienie – są ignorowane): antologia, tytuł, tytuł w bazie,
ostrzeżenia. Tekst szukany jest w antologii po „tytule w bazie” albo po „tytule” (wielkość liter, białe znaki i rodzaj
cudzysłowów nie mają znaczenia). Gdy tekst znaleziono po „tytule w bazie”, a „tytuł” jest inny, tytuł w CMS zostaje
zmieniony na ten z pliku (wersja z antologii).

Domyślnie tylko podgląd (transakcja wycofana). Z --apply zapisuje całość albo nic. Wypełnia tylko puste ostrzeżenia;
inne istniejące zgłasza jako rozbieżność, chyba że podano --overwrite. Wiersze z pustymi ostrzeżeniami są pomijane.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.management.commands.import_audiobook_publications import title_key
from core.normalization import compact
from texts.models import Anthology, Text

HEADER = ('antologia', 'tytuł', 'tytuł w bazie', 'ostrzeżenia')
COUNT_LABELS = (('filled', 'uzupełnione ostrzeżenia'), ('overwritten', 'zastąpione ostrzeżenia'),
                ('renamed', 'zmienione tytuły'), ('unchanged', 'bez zmian'), ('empty', 'wiersze bez ostrzeżeń'))


class RowError(Exception):
    pass


def parse_rows(content):
    """Lista (nr wiersza, dane | None, błąd | None); puste wiersze pomija."""
    numbered = [(number, line) for number, line in enumerate(content.splitlines(), 1) if line.strip()]
    if not numbered:
        raise CommandError('Plik jest pusty.')
    header = tuple(compact(cell).casefold() for cell in numbered[0][1].split('\t'))
    if header[:len(HEADER)] != HEADER:
        raise CommandError('Nieprawidłowy nagłówek. Pierwsze kolumny (rozdzielone tabulatorem): ' + ' | '.join(HEADER) + '.')
    rows = []
    for number, line in numbered[1:]:
        cells = [compact(cell) for cell in line.split('\t')] + [''] * len(HEADER)
        anthology, title, db_title, warnings = cells[:len(HEADER)]
        if not anthology or not (title or db_title):
            rows.append((number, None, 'podaj antologię i tytuł.'))
            continue
        rows.append((number, {'anthology': anthology, 'title': title or db_title, 'db_title': db_title or title,
                              'warnings': warnings}, None))
    return rows


class Command(BaseCommand):
    help = ('Importuje ostrzeżenia dotyczące treści z pliku TSV (antologia, tytuł, tytuł w bazie, ostrzeżenia, …). '
            'Bez --apply tylko podgląd.')

    def add_arguments(self, parser):
        parser.add_argument('path', help='Plik TSV w UTF-8.')
        parser.add_argument('--apply', action='store_true', help='Zapisz zmiany (domyślnie tylko podgląd).')
        parser.add_argument('--overwrite', action='store_true',
                            help='Zastąp istniejące ostrzeżenia w CMS (domyślnie uzupełnia tylko puste).')

    def handle(self, *args, **options):
        try:
            with open(options['path'], encoding='utf-8-sig') as source:
                rows = parse_rows(source.read())
        except (OSError, UnicodeDecodeError) as error:
            raise CommandError(f'Nie można odczytać pliku: {error}') from error
        counts = dict.fromkeys(dict(COUNT_LABELS), 0)
        discrepancies, failures, used = [], [], set()
        with transaction.atomic():
            anthologies = {}
            for anthology in Anthology.objects.only('pk', 'title', 'is_novel'):
                anthologies.setdefault(title_key(anthology.title), []).append(anthology)
            for number, data, error in rows:
                if data and not error:
                    try:
                        text = self.find_text(anthologies, data)
                        if text.pk in used:
                            raise RowError(f'tekst „{text.title}” występuje w pliku więcej niż raz.')
                        used.add(text.pk)
                        lines = self.import_row(text, data, options['overwrite'], counts, discrepancies)
                    except RowError as row_error:
                        error = str(row_error)
                if error:
                    failures.append(f'wiersz {number}: {error}')
                    continue
                for line in lines:
                    self.stdout.write(f'Wiersz {number} – {data["anthology"]}: „{data["title"]}”: {line}')
            if failures or not options['apply']:
                transaction.set_rollback(True)
        self.stdout.write('Podsumowanie: ' + ', '.join(f'{label}: {counts[key]}' for key, label in COUNT_LABELS) + '.')
        if discrepancies:
            self.stdout.write(self.style.WARNING(
                f'Inne ostrzeżenia już w CMS ({len(discrepancies)}) – bez zmian (użyj --overwrite, by zastąpić):'))
            for line in discrepancies:
                self.stdout.write(f'  {line}')
        if failures:
            self.stdout.write(self.style.ERROR(f'Błędy ({len(failures)}):'))
            for line in failures:
                self.stdout.write(f'  {line}')
            raise CommandError(f'Błędne wiersze: {len(failures)}. Nic nie zapisano – popraw plik i uruchom ponownie.')
        self.stdout.write(self.style.SUCCESS('Zapisano.') if options['apply'] else 'Podgląd – nic nie zapisano. Użyj --apply.')

    def find_text(self, anthologies, data):
        found = anthologies.get(title_key(data['anthology']), [])
        if not found:
            raise RowError(f'nie ma antologii „{data["anthology"]}”.')
        if len(found) > 1:
            raise RowError(f'kilka antologii o tytule „{data["anthology"]}”.')
        anthology = found[0]
        if anthology.is_novel:
            raise RowError(f'„{anthology.title}” to powieść – ostrzeżenia ustawia się w profilu powieści.')
        texts = list(Text.objects.filter(anthology=anthology, chapter_number__isnull=True).only('pk', 'title', 'content_warnings'))
        for wanted in (data['db_title'], data['title']):
            matches = [text for text in texts if title_key(text.title) == title_key(wanted)]
            if len(matches) > 1:
                raise RowError(f'kilka tekstów „{wanted}” w antologii „{anthology.title}”.')
            if matches:
                return matches[0]
        raise RowError(f'w antologii „{anthology.title}” nie ma tekstu „{data["db_title"]}”.')

    def import_row(self, text, data, overwrite, counts, discrepancies):
        lines, fields = [], []
        if title_key(text.title) != title_key(data['title']):
            lines.append(f'tytuł „{text.title}” → „{data["title"]}”')
            text.title = data['title']
            fields.append('title')
            counts['renamed'] += 1
        warnings, current = data['warnings'], compact(text.content_warnings)
        if not warnings:
            counts['empty'] += 1
        elif current == warnings:
            pass
        elif not current or overwrite:
            lines.append(('zastąpione ostrzeżenia: ' if current else 'ostrzeżenia: ') + warnings)
            counts['overwritten' if current else 'filled'] += 1
            text.content_warnings = warnings
            fields.append('content_warnings')
        else:
            discrepancies.append(f'{data["anthology"]}: „{text.title}” – CMS: {current} | plik: {warnings}')
        if fields:
            text.save(update_fields=fields)
        elif warnings:
            counts['unchanged'] += 1
        return lines
