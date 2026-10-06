"""Import numbered chapters and their lengths without replacing existing workflow."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import unicodedata

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from texts.models import Anthology, NovelProfile, Text
from texts.novels import new_chapter


def title_key(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def read_input(path):
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or data.get('format') != 'novel-chapter-lengths-v1':
        raise ValueError('Nieobsługiwany format danych rozdziałów.')
    title = data.get('novel_title')
    rows = data.get('chapters')
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        raise ValueError('Brak poprawnego tytułu powieści.')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError('Oczekiwano od 1 do 500 rozdziałów.')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Nieprawidłowy rekord rozdziału.')
        for field in ('chapter_number', 'length'):
            if type(row.get(field)) is not int or not 1 <= row[field] <= 2147483647:
                raise ValueError(f'Nieprawidłowa wartość {field}: {row.get(field)!r}.')
    if sorted(row['chapter_number'] for row in rows) != list(range(1, len(rows) + 1)):
        raise ValueError('Numery rozdziałów muszą być unikatowe i ciągłe od 1.')
    if type(data.get('total_characters_with_spaces')) is not int or data['total_characters_with_spaces'] != sum(row['length'] for row in rows):
        raise ValueError('Suma długości nie zgadza się z danymi rozdziałów.')
    return data, hashlib.sha256(raw).hexdigest()


class Command(BaseCommand):
    help = 'Uzupełnia długości istniejących rozdziałów i dodaje brakujące. Domyślnie tylko podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('input_file')
        parser.add_argument('--apply', action='store_true', help='Zapisz zmiany w jednej transakcji.')
        parser.add_argument('--novel-id', type=int, help='Wybierz konkretny rekord przy powielonym tytule; tytuł nadal musi się zgadzać.')
        parser.add_argument('--report', default='raport_bsd_rozdzialy.json', help='Plik JSON z wynikiem; domyślnie raport_bsd_rozdzialy.json.')

    def handle(self, *args, **options):
        source = Path(options['input_file'])
        destination = Path(options['report'])
        if source.resolve() == destination.resolve():
            raise CommandError('Plik raportu musi być inny niż plik wejściowy.')
        report = {'mode': 'podgląd – nic nie zapisano', 'input_sha256': None,
                  'novel': None, 'planned_counts': {}, 'saved_counts': {},
                  'chapters': [], 'warnings': [], 'errors': []}
        failed = False
        try:
            data, report['input_sha256'] = read_input(source)
            report['counting_method'] = data.get('counting_method', '')
            matches = [(pk, title) for pk, title in Anthology.objects.values_list('pk', 'title')
                       if title_key(title) == title_key(data['novel_title'])]
            if options['novel_id'] is not None:
                matches = [item for item in matches if item[0] == options['novel_id']]
            if len(matches) != 1:
                raise ValueError(f'Powieść „{data["novel_title"]}” musi wskazywać dokładnie jeden istniejący rekord; znaleziono: {len(matches)}. ID zgodnych tytułów: {[pk for pk, _ in matches]}.')
            novel_id = matches[0][0]
            with transaction.atomic():
                # The CMS workflow uses this lock order: texts, then anthology.
                list(Text.objects.select_for_update().filter(anthology_id=novel_id).order_by('pk').values_list('pk', flat=True))
                book = Anthology.objects.select_for_update().get(pk=novel_id)
                if title_key(book.title) != title_key(data['novel_title']):
                    raise ValueError('Tytuł powieści został zmieniony. Uruchom podgląd ponownie.')
                if not book.is_novel or book.is_translated:
                    raise ValueError('Wybrany rekord musi być powieścią bez oznaczenia „Tłumaczone”.')
                profile = NovelProfile.objects.select_for_update().get(anthology=book)
                report['novel'] = {'id': book.pk, 'title': book.title}
                existing = list(Text.objects.filter(anthology=book).order_by('chapter_number', 'pk'))
                if any(text.chapter_number is None for text in existing):
                    raise ValueError('Powieść zawiera rekord bez numeru rozdziału; potrzebna ręczna kontrola.')
                numbers = [text.chapter_number for text in existing]
                if len(numbers) != len(set(numbers)):
                    raise ValueError('W bazie powtarzają się numery rozdziałów; niczego nie scalono.')
                by_number = {text.chapter_number: text for text in existing}
                plans = []
                for row in sorted(data['chapters'], key=lambda item: item['chapter_number']):
                    text = by_number.get(row['chapter_number'])
                    action = 'create' if text is None else 'unchanged' if text.length == row['length'] else 'update_length'
                    plans.append({'chapter_number': row['chapter_number'], 'title': f'Rozdział {row["chapter_number"]}',
                                  'text_id': text.pk if text else None, 'old_length': text.length if text else None,
                                  'new_length': row['length'], 'action': action})
                report['chapters'] = plans
                counts = Counter(plan['action'] for plan in plans)
                report['planned_counts'] = {key: counts[key] for key in ('create', 'update_length', 'unchanged')}
                if counts['create'] and book.status == Anthology.Status.READY:
                    raise ValueError('Powieść ma status Gotowa. Aby dodać brakujące rozdziały, najpierw wznów jej pracę w CMS.')
                if counts['create'] and not profile.authors.exists():
                    report['warnings'].append('Profil powieści nie ma autora; nowe rozdziały odziedziczą tę pustą listę. Import nie tworzy autorów.')
                extras = sorted(set(by_number) - {row['chapter_number'] for row in data['chapters']})
                if extras:
                    report['warnings'].append(f'Rozdziały spoza pliku pozostaną bez zmian: {extras}.')
                if options['apply']:
                    for plan in plans:
                        if plan['action'] == 'create':
                            text = new_chapter(book, profile, chapter_number=plan['chapter_number'], length=plan['new_length'])
                            plan['text_id'] = text.pk
                        elif plan['action'] == 'update_length':
                            text = by_number[plan['chapter_number']]
                            text.length = plan['new_length']
                            text.save(update_fields=['length'])
            if options['apply']:
                report['mode'] = 'zapisano'
                report['saved_counts'] = dict(report['planned_counts'])
        except (OSError, ValueError, ValidationError, DatabaseError, Anthology.DoesNotExist, NovelProfile.DoesNotExist) as exc:
            failed = True
            report['mode'] = 'przerwano – nic nie zapisano'
            report['saved_counts'] = {}
            # Rolled-back IDs of newly created rows are not persistent records.
            for plan in report['chapters']:
                if plan['action'] == 'create':
                    plan['text_id'] = None
            report['errors'].append(str(exc))
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        self.stdout.write(serialized)
        try:
            destination.write_text(serialized + '\n', encoding='utf-8')
        except OSError as exc:
            state = 'Zmiany zostały zapisane w bazie' if report['mode'] == 'zapisano' else 'Nie zapisano zmian w bazie'
            raise CommandError(f'{state}, ale nie można zapisać raportu {destination}: {exc}. Wynik znajduje się powyżej.') from exc
        if failed:
            raise CommandError(f'Import przerwany; nie zapisano zmian. Szczegóły: {destination}.')
        self.stdout.write(f'Raport: {destination}')
