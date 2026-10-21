"""Dopisuje publikacje audiobooków (data premiery, linki, lektor) z pliku TSV do tekstów antologii.

Domyślnie tylko podgląd: wszystkie zapisy wykonuje w transakcji i na końcu ją wycofuje. Z --apply zapisuje
całość albo nic – błąd dowolnego wiersza oznacza brak zapisu. Statusu i etapów istniejących audiobooków nie zmienia;
nowe powstają od razu jako „Opublikowane”, bez etapów. Wpisy w dzienniku zmian nie wysyłają powiadomień Discord.
"""
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q

from core.audiobook_models import AudioContributor, Audiobook, contact_key
from core.audiobook_services import record_event
from core.audiobook_validators import validate_hearthis_url, validate_youtube_url
from core.normalization import compact
from texts.models import Anthology, Text

HEADER = ('antologia', 'autor', 'tytuł', 'data premiery', 'youtube', 'hearthis', 'kolejne części', 'lektor', 'uwagi')
PUBLISHED = Audiobook.Status.PUBLISHED
QUOTES = str.maketrans({'„': '"', '”': '"', '“': '"', '‚': "'", '’': "'", '‘': "'"})
SERVICES = (('youtube', validate_youtube_url), ('hearthis', validate_hearthis_url))
COUNT_LABELS = (('new', 'nowe audiobooki'), ('dates', 'uzupełnione daty premiery'), ('links', 'uzupełnione linki'),
                ('parts', 'dopisane kolejne części'), ('narrators', 'uzupełnieni lektorzy'),
                ('contacts', 'nowe wpisy w spisie lektorów'), ('unchanged', 'bez zmian'))


class RowError(Exception):
    pass


def title_key(value):
    """Klucz porównania tytułów: białe znaki, wielkość liter oraz rodzaj cudzysłowów i apostrofów nie mają znaczenia."""
    return compact(value).casefold().translate(QUOTES)


def parse_date(value):
    try:
        return datetime.strptime(value, '%d.%m.%Y').date()
    except ValueError:
        raise RowError(f'nieprawidłowa data premiery „{value}” – oczekiwano DD.MM.RRRR.') from None


def link_service(url):
    """Usługa (youtube/hearthis) rozpoznana po adresie; inny adres to błąd wiersza."""
    for service, validate in SERVICES:
        try:
            validate(url)
        except ValidationError:
            continue
        return service
    raise RowError(f'nieprawidłowy adres kolejnej części „{url}” – oczekiwano YouTube albo HearThis.')


def parse_rows(text):
    """Zwraca listę (nr wiersza, dane | None, błąd | None). Puste wiersze pomija."""
    lines = text.splitlines()
    numbered = [(number, line) for number, line in enumerate(lines, 1) if line.strip()]
    if not numbered:
        raise CommandError('Plik jest pusty.')
    header = [compact(cell).casefold() for cell in numbered[0][1].split('\t')]
    while header and not header[-1]:
        header.pop()
    if tuple(header) != HEADER:
        raise CommandError('Nieprawidłowy nagłówek. Oczekiwano kolumn rozdzielonych tabulatorem: '
                           + ' | '.join(HEADER) + '.')
    rows = []
    for number, line in numbered[1:]:
        cells = [compact(cell) for cell in line.split('\t')]
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
    anthology, author, title, premiere, youtube, hearthis, parts, narrator, _notes = cells
    if not anthology or not title:
        raise RowError('podaj antologię i tytuł.')
    urls = []
    for url in parts.split('|'):
        url = url.strip()
        if url and url not in urls:
            urls.append(url)
    return {'anthology': anthology, 'author': author, 'title': title,
            'premiere': parse_date(premiere) if premiere else None, 'youtube': youtube, 'hearthis': hearthis,
            'parts': [(link_service(url), url) for url in urls], 'narrator': narrator}


class Command(BaseCommand):
    help = ('Importuje publikacje audiobooków z pliku TSV (antologia, autor, tytuł, data premiery, youtube, hearthis, '
            'kolejne części, lektor, uwagi). Bez --apply tylko podgląd.')

    def add_arguments(self, parser):
        parser.add_argument('path', help='Plik TSV w UTF-8.')
        parser.add_argument('--apply', action='store_true', help='Zapisz zmiany (domyślnie tylko podgląd).')
        parser.add_argument('--overwrite-premiere', action='store_true',
                            help='Zastąp datę premiery z CMS datą z pliku, gdy się różnią (domyślnie tylko uzupełnia pustą).')
        parser.add_argument('--user', help='E-mail konta wpisywanego w dzienniku zmian (domyślnie pierwszy superuser).')

    def handle(self, *args, **options):
        try:
            with open(options['path'], encoding='utf-8-sig') as source:
                rows = parse_rows(source.read())
        except (OSError, UnicodeDecodeError) as error:
            raise CommandError(f'Nie można odczytać pliku: {error}') from error
        self.user = self.resolve_user(options['user'])
        self.overwrite = options['overwrite_premiere']
        self.counts = dict.fromkeys(dict(COUNT_LABELS), 0)
        self.discrepancies, failures = [], []
        with transaction.atomic():
            self.anthologies = {}
            for anthology in Anthology.objects.only('pk', 'title'):
                self.anthologies.setdefault(title_key(anthology.title), []).append(anthology)
            self.contacts = list(AudioContributor.objects.only('pk', 'name'))
            for number, data, error in rows:
                self.stdout.write(f'Wiersz {number}' + (f' – {data["anthology"]}: „{data["title"]}”:' if data else ':'))
                if data:
                    before = dict(self.counts), list(self.discrepancies), list(self.contacts)
                    try:
                        # Punkt zapisu: błędny wiersz nie zostawia częściowych zmian w podglądzie.
                        with transaction.atomic():
                            lines = self.import_row(number, data)
                    except RowError as row_error:
                        error = str(row_error)
                    except (ValidationError, IntegrityError) as save_error:
                        messages = getattr(save_error, 'messages', None) or [str(save_error)]
                        error = 'zapis odrzucony: ' + ' '.join(messages)
                    if error:
                        self.counts, self.discrepancies, self.contacts = before
                if error:
                    failures.append(f'wiersz {number}: {error}')
                    self.stdout.write(f'  BŁĄD: {error}')
                else:
                    for line in lines:
                        self.stdout.write(f'  {line}')
            if failures or not options['apply']:
                transaction.set_rollback(True)
        self.stdout.write('Podsumowanie: ' + ', '.join(f'{label}: {self.counts[key]}' for key, label in COUNT_LABELS) + '.')
        if self.discrepancies:
            self.stdout.write(self.style.WARNING(f'Rozbieżności ({len(self.discrepancies)}) – bez zmian w CMS:'))
            for line in self.discrepancies:
                self.stdout.write(f'  {line}')
        if failures:
            self.stdout.write(self.style.ERROR(f'Błędy ({len(failures)}):'))
            for line in failures:
                self.stdout.write(f'  {line}')
            raise CommandError(f'Błędne wiersze: {len(failures)}. Nic nie zapisano – popraw plik i uruchom ponownie.')
        if options['apply']:
            self.stdout.write(self.style.SUCCESS('Zapisano.'))
        else:
            self.stdout.write('Podgląd – nic nie zapisano. Użyj --apply.')

    def resolve_user(self, email):
        """Konto wpisywane w dzienniku: z --user (e-mail albo nazwa użytkownika) lub pierwszy superuser."""
        users = get_user_model().objects
        if email:
            user = users.filter(Q(email__iexact=email.strip()) | Q(username__iexact=email.strip())).order_by('pk').first()
            if user is None:
                raise CommandError(f'Nie ma konta z adresem „{email}”.')
            return user
        user = users.filter(is_superuser=True).order_by('pk').first()
        if user is None:
            raise CommandError('Brak konta superusera – podaj konto przez --user.')
        return user

    def find_text(self, data):
        """Tekst po tytule w antologii; kilka tekstów o tym tytule rozstrzyga kolumna autora."""
        anthologies = self.anthologies.get(title_key(data['anthology']), [])
        if not anthologies:
            raise RowError(f'nie ma antologii „{data["anthology"]}”.')
        if len(anthologies) > 1:
            raise RowError(f'kilka antologii o tytule „{data["anthology"]}” (ID: '
                           + ', '.join(str(item.pk) for item in anthologies) + ').')
        key = title_key(data['title'])
        texts = [text for text in Text.objects.filter(anthology=anthologies[0]).prefetch_related('authors')
                 if title_key(text.title) == key]
        if not texts:
            raise RowError(f'w antologii „{anthologies[0].title}” nie ma tekstu „{data["title"]}”.')
        if len(texts) > 1:
            author = title_key(data['author'])
            texts = [text for text in texts if author and any(
                title_key(person.display_name) == author or title_key(str(person)) == author for person in text.authors.all())]
            if len(texts) != 1:
                raise RowError(f'kilka tekstów „{data["title"]}” w antologii „{anthologies[0].title}” – nie rozstrzyga ich '
                               f'kolumna autora „{data["author"]}”.')
        return texts[0]

    def find_contact(self, name):
        """Lektor ze spisu (dopasowanie jak przy dopisywaniu w formularzu); brak – nowy wpis w spisie."""
        matches = [contact for contact in self.contacts if contact_key(contact.name) == contact_key(name)]
        if len(matches) > 1:
            raise RowError(f'kilka wpisów „{name}” w spisie lektorów (ID: ' + ', '.join(str(c.pk) for c in matches)
                           + ') – scal je komendą merge_audio_contributor.')
        if matches:
            return matches[0], False
        contact = AudioContributor(name=name)
        contact.full_clean()
        contact.save()
        self.contacts.append(contact)
        self.counts['contacts'] += 1
        return contact, True

    def add_parts(self, audio, parts):
        """Dopisuje kolejne części, których jeszcze nie ma; numer części liczony osobno dla każdej usługi."""
        present = {getattr(audio, f'{service}_url') for service, _ in SERVICES} | {link['url'] for link in audio.additional_links}
        added = []
        for service, url in parts:
            if url in present:
                continue
            number = 1 + bool(getattr(audio, f'{service}_url')) + sum(
                1 for link in audio.additional_links if link['service'] == service)
            audio.additional_links = [*audio.additional_links, {'service': service, 'part': number, 'url': url}]
            present.add(url)
            added.append(f'{service} cz. {number}')
        return added

    def note(self, number, text, message):
        self.discrepancies.append(f'wiersz {number}, „{text.title}”: {message}')
        return f'rozbieżność: {message}'

    def import_row(self, number, data):
        text = self.find_text(data)
        audio = Audiobook.objects.select_for_update().filter(text=text).first()
        contact = new_contact = None
        if data['narrator']:
            contact, new_contact = self.find_contact(data['narrator'])
        lines = [f'nowy wpis w spisie lektorów: „{contact.name}”'] if new_contact else []
        if audio is None:
            audio = Audiobook(text=text, status=PUBLISHED, premiere_date=data['premiere'], youtube_url=data['youtube'],
                              hearthis_url=data['hearthis'], narrator_contact=contact)
            parts = self.add_parts(audio, data['parts'])
            audio.full_clean()
            audio.save()
            self.counts['new'] += 1
            details = ['Import publikacji: audiobook opublikowany'
                       + (f' {data["premiere"]:%d.%m.%Y}' if data['premiere'] else ' (data premiery nieznana)')]
            if data['youtube'] or data['hearthis'] or parts:
                details.append('linki: ' + ', '.join(filter(None, ['YouTube' if data['youtube'] else '',
                                                                   'HearThis' if data['hearthis'] else '', *parts])))
            if contact:
                details.append(f'lektor: {contact.name}')
            record_event(text, self.user, previous='–', current=audio.status_label, details='; '.join(details) + '.',
                         notify=False)
            return lines + [f'nowy audiobook (Opublikowane): {"; ".join(details[1:]) or "bez linków"}']
        lines.append(f'istniejący audiobook #{audio.pk}')
        if audio.status != PUBLISHED:
            lines.append(self.note(number, text, f'status audiobooka w CMS: {audio.status_label} (nie Opublikowane)'))
        changes = []
        if data['premiere']:
            if audio.premiere_date is None:
                audio.premiere_date = data['premiere']
                self.counts['dates'] += 1
                changes.append(f'data premiery {audio.premiere_date:%d.%m.%Y}')
            elif audio.premiere_date != data['premiere']:
                if self.overwrite:
                    changes.append(f'data premiery {audio.premiere_date:%d.%m.%Y} → {data["premiere"]:%d.%m.%Y}')
                    audio.premiere_date = data['premiere']
                    self.counts['dates'] += 1
                else:
                    lines.append(self.note(number, text, f'różna data premiery: CMS {audio.premiere_date:%d.%m.%Y} '
                                                         f'/ plik {data["premiere"]:%d.%m.%Y}'))
        for service, label in (('youtube', 'YouTube'), ('hearthis', 'HearThis')):
            current, incoming = getattr(audio, f'{service}_url'), data[service]
            if not incoming:
                continue
            if not current:
                setattr(audio, f'{service}_url', incoming)
                self.counts['links'] += 1
                changes.append(f'link {label}')
            elif current != incoming:
                lines.append(self.note(number, text, f'różny link {label}: CMS {current} / plik {incoming}'))
        parts = self.add_parts(audio, data['parts'])
        if parts:
            self.counts['parts'] += len(parts)
            changes.append('kolejne części: ' + ', '.join(parts))
        if contact:
            if audio.narrator_contact_id is None:
                audio.narrator_contact = contact
                self.counts['narrators'] += 1
                changes.append(f'lektor {contact.name}')
            elif audio.narrator_contact_id != contact.pk:
                lines.append(self.note(number, text, f'inny lektor: CMS {audio.narrator_name} / plik {data["narrator"]}'))
        if not changes:
            self.counts['unchanged'] += 1
            return lines + ['bez zmian']
        audio.full_clean()
        audio.save()
        record_event(text, self.user, previous=audio.status_label, current=audio.status_label,
                     details='Import publikacji: ' + ', '.join(changes) + '.', notify=False)
        return lines + ['uzupełniono: ' + ', '.join(changes)]
