"""Wysyłka autorom plików po podziale DOCX (Programy → Podział DOCX → Wyślij autorom).

Pliki pochodzą z wyniku zadania podziału (archiwum ZIP w katalogu zadania programu), więc dokumentu
nie trzeba wgrywać ponownie. Partia – ustawienia wiadomości i lista kontrolna – leży w katalogu
zadania i znika razem z nim. Wysyłka zakłada wyłącznie (O_EXCL) znacznik SENT: drugie zatwierdzenie
tej samej partii (podwójne kliknięcie, druga karta) niczego nie wysyła.
"""
import os
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
            .select_related('anthology').prefetch_related('authors', 'translation__translators')
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
               'people': [], 'emails': [], 'missing': [], 'status': NO_MATCH, 'note': ''}
        candidates = by_title.get(key, []) if key else []
        if len(candidates) > 1:
            row['note'] = 'Kilka tekstów antologii ma ten tytuł.'
        elif candidates and keys.count(key) > 1:
            row['note'] = 'Ten sam nagłówek ma kilka części dokumentu.'
        elif candidates:
            text = candidates[0]
            people, emails, missing = recipients(text, translated)
            row.update(text_id=text.pk, title=text.title, people=people, emails=emails, missing=missing,
                       status=READY if emails else NO_ADDRESS)
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


def prepare(folder, anthology, subject, message):
    """Zapisuje nową partię (ustawienia i listę kontrolną); poprzednia lista kontrolna przestaje być ważna."""
    if (folder / SENT).exists():
        raise MailingError('Pliki z tego podziału zostały już wysłane.')
    rows = build_rows(read_parts(folder), anthology)
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
    """Wysyła zaznaczone gotowe wiersze – każdy e-mail z jednym plikiem swojego tekstu – i zapisuje raport."""
    rows = [row for row in batch['rows'] if row.get('status') == READY and str(row.get('index')) in selected]
    if not rows:
        raise MailingError('Zaznacz co najmniej jeden wiersz o stanie „Gotowe”.')
    try:
        with (folder / SENT).open('x'):
            pass
    except FileExistsError:
        raise MailingError('Ta partia została już wysłana – nie wysyłam jej drugi raz.') from None
    results = []
    try:
        with ZipFile(folder / 'result') as archive:
            connection = smtp_connection(mailbox)
            connection.open()
            try:
                for row in rows:
                    results.append(_send_row(archive, connection, mailbox, batch, row))
            finally:
                connection.close()
    except (OSError, BadZipFile, smtplib.SMTPException, InvalidToken) as error:
        if isinstance(error, (BadZipFile, FileNotFoundError)):
            message = 'Nie można odczytać plików podziału. Uruchom podział ponownie.'
        else:
            message = smtp_error(error)
        done = {row['index'] for row in results}
        results += [{'index': row['index'], 'sent': False, 'error': message} for row in rows if row['index'] not in done]
    sent = sum(result['sent'] for result in results)
    by_index = {result['index']: result for result in results}
    report = {'anthology_title': batch.get('anthology_title', ''), 'sent': sent, 'failed': len(results) - sent,
              'finished': time.time(),
              'rows': [{**row, **by_index[row['index']]} for row in rows]}
    if sent:
        jobs.write_json(folder / REPORT, report)
        # Pliki wysłane – archiwum nie jest już potrzebne na serwerze.
        (folder / 'result').unlink(missing_ok=True)
    else:
        # Nic nie wyszło (np. zły serwer SMTP): partię można poprawić i wysłać ponownie.
        (folder / SENT).unlink(missing_ok=True)
    return report


def _send_row(archive, connection, mailbox, batch, row):
    try:
        payload = archive.read(row['file'])
        message = EmailMessage(batch['subject'], batch['message'], from_email=mailbox.username,
                               to=row['emails'], connection=connection)
        message.attach(attachment_name(row['file']), payload, jobs.DOCX_MIME)
        message.send()
    except KeyError:
        return {'index': row['index'], 'sent': False, 'error': 'Brak pliku w archiwum podziału.'}
    except (OSError, smtplib.SMTPException) as error:
        return {'index': row['index'], 'sent': False, 'error': smtp_error(error)}
    return {'index': row['index'], 'sent': True, 'error': ''}
