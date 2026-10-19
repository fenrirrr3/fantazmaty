"""Wysyłka autorom plików po podziale DOCX (Programy → Podział DOCX → Wyślij autorom).

Pliki pochodzą z wyniku zadania podziału (archiwum ZIP w katalogu zadania programu), więc dokumentu
nie trzeba wgrywać ponownie. Wiersze o tym samym zbiorze odbiorców idą w jednym mailu (kilka załączników, suma
do MAX_MAIL_BYTES); mail testowy idzie na adres właściciela i partii nie zmienia. Partia – ustawienia wiadomości i lista kontrolna – leży w katalogu
zadania i znika razem z nim. Wysyłka zakłada wyłącznie (O_EXCL) znacznik SENT: drugie zatwierdzenie
tej samej partii (podwójne kliknięcie, druga karta) niczego nie wysyła.
"""
import os
import random
import re
import secrets
import smtplib
import time
import unicodedata
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from cryptography.fernet import InvalidToken
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.db.models import F, Q

from core.models import MailboxConnection
from core.services import program_jobs as jobs

SALT = 'split-mailing-batch-v1'
BATCH = 'mailing.json'
SENT = 'mailing-sent'
REPORT = 'mailing-report.json'
SMTP_TIMEOUT = 30
# Suma załączników jednej wiadomości; większa grupa idzie w kilku mailach, większy plik – sam.
MAX_MAIL_BYTES = 14 * 1024 * 1024  # surowe pliki; po kodowaniu base64 ok. 19 MB – poniżej typowych limitów SMTP
TEST_PREFIX = '[TEST] '
SUBJECT_LIMIT = 255
# Tagi w temacie i treści, uzupełniane osobno dla każdego maila (wielkość liter bez znaczenia).
TAGS = {
    'antologia': 'tytuł antologii',
    'tytuł-tekstu': 'tytuł tekstu (kilka tekstów w jednym mailu: „A i B”, „A, B i C”)',
    'autor': 'podpis autora jak w stopce (w przekładzie – autor oryginału)',
}
# Tag: do 30 znaków w nawiasach kwadratowych, bez białych znaków – „[w załączniku plik]” zostaje zwykłym tekstem.
TAG_RE = re.compile(r'\[([^\[\]\s]{1,30})\]')
READY, NO_MATCH, NO_ADDRESS = 'ready', 'no_match', 'no_address'
STATUS_LABELS = {READY: 'Gotowe', NO_MATCH: 'Brak dopasowania', NO_ADDRESS: 'Brak adresu'}
NO_MAILBOX = ('Brak skrzynki do wysyłki. W panelu administratora (Skrzynki zgłoszeń) dodaj skrzynkę z przeznaczeniem '
              '„Wysyłka plików autorom” i podaj jej serwer SMTP.')
MANY_MAILBOXES = ('Aktywnych skrzynek „Wysyłka plików autorom” jest kilka. W panelu administratora '
                  '(Skrzynki zgłoszeń) zostaw aktywną tylko jedną.')


class MailingError(ValueError):
    pass


def mailing_mailbox():
    """Jedyna aktywna skrzynka z przeznaczeniem „Wysyłka plików autorom”."""
    boxes = list(MailboxConnection.objects.filter(purpose=MailboxConnection.Purpose.SPLIT_MAILING, is_active=True)[:2])
    if len(boxes) != 1:
        raise MailingError(MANY_MAILBOXES if boxes else NO_MAILBOX)
    return boxes[0]


def normalized_title(value):
    """Tytuł do porównania: bez wielkości liter, białych znaków, cudzysłowów i interpunkcji (ogonki zostają)."""
    return ''.join(char for char in unicodedata.normalize('NFKC', value or '').casefold() if char.isalnum())


def anthology_texts(anthology):
    """Teksty antologii bez wycofanych, w kolejności listy tekstów antologii (jak stopki)."""
    from core.selectors.texts import _annotated_texts

    return (_annotated_texts(include_translations=True, include_novels=True)
            .filter(anthology=anthology)
            .filter(Q(current_stage_type__isnull=True) | ~Q(current_stage_type='withdrawn'))
            .select_related('anthology').prefetch_related('authors', 'translation__translators', 'translation__foreign_authors')
            .order_by(F('chapter_number').asc(nulls_last=True), 'title', 'pk'))


def recipients(text, translated):
    """Osoby, do których idzie plik: autorzy tekstu, a w antologii tłumaczonej – tłumacze."""
    if translated:
        try:
            people = list(text.translation.translators.all())
        except ObjectDoesNotExist:
            people = []
    else:
        people = list(text.authors.all())
    emails = list(dict.fromkeys((person.email or '').strip() for person in people if (person.email or '').strip()))
    missing = [person.display_name for person in people if not (person.email or '').strip()]
    return [person.display_name for person in people], emails, missing


def signature(text, translated):
    """Podpis autorów jak w stopce: autorzy tekstu, w antologii tłumaczonej – autorzy oryginału."""
    if translated:
        try:
            people = list(text.translation.foreign_authors.all())
        except ObjectDoesNotExist:
            people = []
    else:
        people = list(text.authors.all())
    return [person.display_name for person in people]


def unknown_tags(value):
    """Tagi spoza TAGS (np. literówka „[tytul]”), w kolejności wystąpienia."""
    return list(dict.fromkeys(f'[{tag}]' for tag in TAG_RE.findall(value) if tag.casefold() not in TAGS))


def join_list(items):
    """„A”, „A i B”, „A, B i C”."""
    items = list(items)
    return ' i '.join(filter(None, [', '.join(items[:-1]), items[-1] if items else '']))


def fill_tags(value, values):
    return TAG_RE.sub(lambda match: values.get(match.group(1).casefold(), match.group(0)), value)


def mail_content(batch, rows, prefix=''):
    """Temat (jeden wiersz, ≤ SUBJECT_LIMIT, bez łamania słów) i treść maila z uzupełnionymi tagami."""
    values = {
        'antologia': batch.get('anthology_title', ''),
        'tytuł-tekstu': join_list(row.get('title') or row.get('heading') or '' for row in rows),
        'autor': join_list(dict.fromkeys(name for row in rows for name in row.get('authors', []))),
    }
    subject = prefix + ' '.join(fill_tags(batch['subject'], values).split())
    if len(subject) > SUBJECT_LIMIT:
        cut = subject[:SUBJECT_LIMIT - 1]
        if subject[SUBJECT_LIMIT - 1] != ' ' and ' ' in cut:
            cut = cut.rsplit(' ', 1)[0]
        subject = cut.rstrip() + '…'
    return subject, fill_tags(batch['message'], values)


def _heading(part):
    """Nagłówek części; starsze wyniki bez tytułów – nazwa pliku bez numeru."""
    title = part.get('title') or ''
    return title or Path(part['file']).stem.split(' ', 1)[-1]


def build_rows(parts, anthology):
    """Lista kontrolna: część podziału ↔ tekst antologii o tym samym tytule (bez dopasowań „na siłę”)."""
    translated = anthology.is_translated
    by_title = {}
    for text in anthology_texts(anthology):
        by_title.setdefault(normalized_title(text.title), []).append(text)
    keys = [normalized_title(_heading(part)) for part in parts]
    rows = []
    for index, (part, key) in enumerate(zip(parts, keys)):
        row = {'index': index, 'file': part['file'], 'heading': _heading(part), 'text_id': None, 'title': '',
               'people': [], 'authors': [], 'emails': [], 'missing': [], 'status': NO_MATCH, 'note': ''}
        candidates = by_title.get(key, []) if key else []
        if len(candidates) > 1:
            row['note'] = 'Kilka tekstów antologii ma ten tytuł.'
        elif candidates and keys.count(key) > 1:
            row['note'] = 'Ten sam nagłówek ma kilka części dokumentu.'
        elif candidates:
            text = candidates[0]
            people, emails, missing = recipients(text, translated)
            row.update(text_id=text.pk, title=text.title, people=people, emails=emails, missing=missing,
                       authors=signature(text, translated), status=READY if emails else NO_ADDRESS)
            if not people:
                row['note'] = 'Tekst nie ma przypisanych tłumaczy.' if translated else 'Tekst nie ma przypisanych autorów.'
        rows.append(row)
    return rows


def read_parts(folder):
    try:
        parts = jobs.read_json(folder / jobs.SPLIT_PARTS)
    except (OSError, ValueError):
        parts = None
    if not isinstance(parts, list) or not parts or not all(
            isinstance(part, dict) and isinstance(part.get('file'), str) for part in parts):
        raise MailingError('Brak listy plików z tego podziału. Uruchom podział ponownie.')
    return parts


def read_batch(folder):
    try:
        batch = jobs.read_json(folder / BATCH)
    except (OSError, ValueError):
        return None
    return batch if isinstance(batch, dict) and isinstance(batch.get('rows'), list) else None


def read_report(folder):
    try:
        report = jobs.read_json(folder / REPORT)
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) and isinstance(report.get('rows'), list) else None


def recipient_key(emails):
    """Zbiór odbiorców bez wielkości liter i kolejności – wiersze o tym samym kluczu idą w jednym mailu."""
    return tuple(sorted({email.strip().lower() for email in emails if email.strip()}))


def mail_plan(rows, size_of):
    """Wiersze pogrupowane w maile: ten sam zbiór odbiorców, kolejność listy, suma plików ≤ MAX_MAIL_BYTES."""
    groups = {}
    for row in rows:
        groups.setdefault(recipient_key(row['emails']), []).append(row)
    mails = []
    for group in groups.values():
        current, total = [], 0
        for row in group:
            size = size_of(row)
            if current and total + size > MAX_MAIL_BYTES:
                mails.append(current)
                current, total = [], 0
            current.append(row)
            total += size
        mails.append(current)
    # Kolejność maili według pierwszego wiersza (jak na liście kontrolnej).
    return sorted(mails, key=lambda mail: mail[0]['index'])


def planned_mails(batch):
    """Maile listy kontrolnej (przy zaznaczeniu wszystkich gotowych): [(numer, wiersze, temat)]."""
    ready = [row for row in batch['rows'] if row.get('status') == READY]
    return [(number, mail, mail_content(batch, mail)[0])
            for number, mail in enumerate(mail_plan(ready, lambda row: row.get('size', 0)), 1)]


def _sizes(folder):
    """Rozmiary plików w archiwum podziału (do podziału grup na maile ≤ MAX_MAIL_BYTES)."""
    try:
        with ZipFile(folder / 'result') as archive:
            return {info.filename: info.file_size for info in archive.infolist()}
    except (OSError, BadZipFile):
        return {}


def prepare(folder, anthology, subject, message):
    """Zapisuje nową partię (ustawienia i listę kontrolną); poprzednia lista kontrolna przestaje być ważna."""
    if (folder / SENT).exists():
        raise MailingError('Pliki z tego podziału zostały już wysłane.')
    rows = build_rows(read_parts(folder), anthology)
    sizes = _sizes(folder)
    for row in rows:
        row['size'] = sizes.get(row['file'], 0)
    batch = secrets.token_hex(16)
    jobs.write_json(folder / BATCH, {
        'batch': batch, 'anthology': anthology.pk, 'anthology_title': anthology.title,
        'translated': anthology.is_translated, 'subject': subject, 'message': message, 'rows': rows})
    # Partia przedłuża życie zadania: sprzątanie liczy czas od request.json.
    os.utime(folder / 'request.json', None)


def batch_token(user, batch):
    """Podpisany identyfikator partii w formularzu listy kontrolnej (wysyłka tylko tej, którą widział użytkownik)."""
    return signing.dumps({'batch': batch['batch'], 'user': user.pk}, salt=SALT)


def checked_batch(folder, user, token):
    """Partia z listy kontrolnej, o ile podpis jest ważny i dotyczy aktualnej partii tego użytkownika."""
    batch = read_batch(folder)
    try:
        value = signing.loads(token or '', salt=SALT, max_age=jobs.TTL)
    except signing.BadSignature:
        value = {}
    if batch is None or value.get('user') != user.pk or value.get('batch') != batch.get('batch'):
        raise MailingError('Lista kontrolna wygasła albo została zmieniona. Sprawdź ją ponownie przed wysyłką.')
    return batch


def smtp_connection(mailbox):
    from django.core.mail.backends.smtp import EmailBackend

    ssl = mailbox.smtp_security == MailboxConnection.Security.SSL
    return EmailBackend(host=mailbox.smtp_host, port=mailbox.smtp_port, username=mailbox.username,
                        password=mailbox.get_password(), use_ssl=ssl, use_tls=not ssl,
                        timeout=SMTP_TIMEOUT, fail_silently=False)


def smtp_error(error):
    """Komunikat błędu SMTP po polsku – bez haseł i treści wiadomości."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return 'Serwer SMTP odrzucił login lub hasło skrzynki.'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'Serwer SMTP odrzucił adres odbiorcy.'
    if isinstance(error, InvalidToken):
        return 'Nie można odczytać hasła skrzynki. Zapisz je ponownie w panelu administratora.'
    if isinstance(error, (TimeoutError, ConnectionError)):
        return 'Serwer SMTP nie odpowiada. Sprawdź serwer, port i szyfrowanie.'
    if isinstance(error, smtplib.SMTPException):
        return f'Błąd serwera SMTP: {str(error)[:200]}'
    return 'Nie można połączyć się z serwerem SMTP. Sprawdź serwer, port i szyfrowanie.'


def attachment_name(file):
    """Nazwa załącznika: tytuł z nazwy pliku podziału, bez numeru kolejnego („01 ”)."""
    stem, _, rest = file.partition(' ')
    return rest if stem.isdecimal() and rest else file


def send(folder, batch, selected, mailbox):
    """Wysyła zaznaczone gotowe wiersze – jeden mail na zbiór odbiorców z plikami ich tekstów – i zapisuje raport."""
    rows = [row for row in batch['rows'] if row.get('status') == READY and str(row.get('index')) in selected]
    if not rows:
        raise MailingError('Zaznacz co najmniej jeden wiersz o stanie „Gotowe”.')
    try:
        with (folder / SENT).open('x'):
            pass
    except FileExistsError:
        raise MailingError('Ta partia została już wysłana – nie wysyłam jej drugi raz.') from None
    results, mails = {}, 0
    try:
        with ZipFile(folder / 'result') as archive:
            names = set(archive.namelist())
            for row in rows:
                if row['file'] not in names:
                    results[row['index']] = {'sent': False, 'error': 'Brak pliku w archiwum podziału.', 'mail': None}
            plan = mail_plan([row for row in rows if row['file'] in names],
                             lambda row: archive.getinfo(row['file']).file_size)
            connection = smtp_connection(mailbox)
            connection.open()
            try:
                for number, group in enumerate(plan, 1):
                    error = _send_mail(archive, connection, mailbox, *mail_content(batch, group),
                                       group[0]['emails'], [row['file'] for row in group])
                    mails += not error
                    results.update({row['index']: {'sent': not error, 'error': error, 'mail': number}
                                    for row in group})
            finally:
                connection.close()
    except (OSError, BadZipFile, smtplib.SMTPException, InvalidToken) as error:
        if isinstance(error, (BadZipFile, FileNotFoundError)):
            message = 'Nie można odczytać plików podziału. Uruchom podział ponownie.'
        else:
            message = smtp_error(error)
        for row in rows:
            results.setdefault(row['index'], {'sent': False, 'error': message, 'mail': None})
    sent = sum(result['sent'] for result in results.values())
    report = {'anthology_title': batch.get('anthology_title', ''), 'sent': sent, 'failed': len(rows) - sent,
              'mails': mails, 'finished': time.time(),
              'rows': [{**row, **results[row['index']]} for row in rows]}
    if sent:
        jobs.write_json(folder / REPORT, report)
        # Pliki wysłane – archiwum nie jest już potrzebne na serwerze.
        (folder / 'result').unlink(missing_ok=True)
    else:
        # Nic nie wyszło (np. zły serwer SMTP): partię można poprawić i wysłać ponownie.
        (folder / SENT).unlink(missing_ok=True)
    return report


def _send_mail(archive, connection, mailbox, subject, body, to, files):
    """Jeden mail z plikami; zwraca komunikat błędu albo ''."""
    try:
        message = EmailMessage(subject, body, from_email=mailbox.username, to=to, connection=connection)
        for file in files:
            message.attach(attachment_name(file), archive.read(file), jobs.DOCX_MIME)
        message.send()
    except (OSError, smtplib.SMTPException) as error:
        return smtp_error(error)
    return ''


def send_test(folder, batch, address, mailbox):
    """Jeden mail na adres testowy z plikiem losowego gotowego wiersza; partii nie oznacza jako wysłanej.

    Zwraca nazwę wysłanego załącznika."""
    if (folder / SENT).exists():
        raise MailingError('Pliki z tej partii zostały już wysłane – test nie jest już możliwy.')
    try:
        with ZipFile(folder / 'result') as archive:
            names = set(archive.namelist())
            with_file = [row for row in batch['rows'] if row.get('file') in names]
            ready = [row for row in with_file if row.get('status') == READY]
            if not with_file:
                raise MailingError('Brak plików do wysłania w teście.')
            row = random.choice(ready or with_file)
            connection = smtp_connection(mailbox)
            connection.open()
            try:
                error = _send_mail(archive, connection, mailbox, *mail_content(batch, [row], TEST_PREFIX),
                                   [address], [row['file']])
            finally:
                connection.close()
    except (BadZipFile, FileNotFoundError):
        raise MailingError('Nie można odczytać plików podziału. Uruchom podział ponownie.') from None
    except (OSError, smtplib.SMTPException, InvalidToken) as error:
        raise MailingError(smtp_error(error)) from None
    if error:
        raise MailingError(error)
    return attachment_name(row['file'])
