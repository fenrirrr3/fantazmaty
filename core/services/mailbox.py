"""Read-only IMAP headers with stable UID cursors and modified UTF-7 folders."""
from datetime import date
import base64
import imaplib
import re
import ssl
from email import policy
from zoneinfo import ZoneInfo
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from django.utils.formats import date_format
from django.utils.translation import override
from cryptography.fernet import InvalidToken

PAGE_SIZE = 50


class MailboxError(Exception):
    pass


def encode_folder(value):
    """RFC 3501 5.1.3, not ordinary UTF-7 (which uses + and /)."""
    output, pending = [], []
    def flush():
        if pending:
            encoded = base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',')
            output.append('&' + encoded + '-')
            pending.clear()
    for char in value:
        if 0x20 <= ord(char) <= 0x7e:
            flush()
            output.append('&-' if char == '&' else char)
        else:
            pending.append(char)
    flush()
    return ''.join(output)


def _positive(value):
    if type(value) is not int or not 0 < value <= 4294967295:
        raise MailboxError('Nieprawidłowy zakres wiadomości. Pobierz nagłówki od początku.')
    return value


def read_headers(config, cursor=None, excluded=None, subject_filter="", *, recruitment_roles=None, sent_since=""):
    if subject_filter and subject_filter not in config.subject_choices():
        raise MailboxError("Wybierz nabór zapisany w ustawieniach skrzynki.")
    from core.recruitment_roles import ROLE_CHOICES
    role_labels = dict(ROLE_CHOICES)
    if recruitment_roles is not None:
        if any(role not in role_labels for role in recruitment_roles) or subject_filter:
            raise MailboxError('Wybierz co najmniej jedną rolę rekrutacyjną z listy.')
    date_search = []
    if sent_since:
        try:
            cutoff = date.fromisoformat(sent_since)
        except (ValueError, TypeError):
            raise MailboxError('Podaj poprawną datę początkową.') from None
        months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
        date_search = ['SENTSINCE', f'{cutoff.day:02d}-{months[cutoff.month - 1]}-{cutoff.year:04d}']
    client = None
    try:
        password = config.get_password()
        context = ssl.create_default_context()
        if config.security == 'ssl':
            client = imaplib.IMAP4_SSL(config.host, config.port, ssl_context=context, timeout=15)
        elif config.security == 'starttls':
            client = imaplib.IMAP4(config.host, config.port, timeout=15)
            client.starttls(ssl_context=context)
        else:
            raise MailboxError('Nieobsługiwany sposób szyfrowania połączenia.')
        try:
            client.login(config.username, password)
        except imaplib.IMAP4.error:
            raise MailboxError('Nie udało się zalogować do skrzynki. Sprawdź login, hasło aplikacji i dostęp do IMAP.') from None
        folder = encode_folder(config.folder)
        folder = '"' + folder.replace('\\', '\\\\').replace('"', '\\"') + '"'
        status, data = client.select(folder, readonly=True)
        if status != 'OK':
            raise MailboxError('Nie można otworzyć folderu. Sprawdź jego nazwę w ustawieniach skrzynki.')
        _, validity_data = client.response('UIDVALIDITY')
        if not validity_data or not validity_data[0]:
            raise MailboxError('Serwer nie podał UIDVALIDITY. Nie można bezpiecznie przeglądać kolejnych stron.')
        validity = _positive(int(validity_data[0]))
        anchor = None
        if cursor:
            if not isinstance(cursor, dict) or cursor.get('validity') != validity:
                raise MailboxError('Folder został zmieniony na serwerze. Pobierz nagłówki od początku.')
            anchor = _positive(cursor.get('anchor'))
            boundary = _positive(cursor.get('boundary'))
            if cursor.get('direction') not in ('older', 'newer'):
                raise MailboxError('Nieprawidłowy kierunek przeglądania.')
        # Stable upper UID excludes arrivals after the first page. UID search
        # remains correct if another mail client deletes messages meanwhile.
        uid_range = f'1:{anchor}' if anchor else '1:*'
        if recruitment_roles:
            # IMAP SUBJECT is a case-insensitive substring search. Union retains
            # messages matching several selected roles without duplicating them.
            matching = set()
            for role in dict.fromkeys(recruitment_roles):
                quoted = ('"' + role_labels[role] + '"').encode('utf-8')
                status, blocks = client.uid('SEARCH', 'CHARSET', 'UTF-8', 'UID', uid_range, *date_search, 'SUBJECT', quoted)
                if status != 'OK':
                    raise MailboxError('Serwer odrzucił wyszukiwanie ról w temacie wiadomości.')
                matching.update(uid for block in blocks or [] if block for uid in block.split())
            status, data = 'OK', [b' '.join(matching)]
        elif subject_filter:
            phrase = f'Nabór: „{subject_filter}”'
            quoted = ('"' + phrase.replace('\\', '\\\\').replace('"', '\\"') + '"').encode('utf-8')
            try:
                status, data = client.uid('SEARCH', 'CHARSET', 'UTF-8', 'UID', uid_range, *date_search, 'SUBJECT', quoted)
            except imaplib.IMAP4.error:
                raise MailboxError('Serwer odrzucił wyszukiwanie tematu w UTF-8. Spróbuj pobrać nagłówki bez filtra naboru.') from None
            if status != 'OK':
                raise MailboxError('Serwer odrzucił wyszukiwanie tematu w UTF-8. Spróbuj pobrać nagłówki bez filtra naboru.')
        else:
            status, data = client.uid('SEARCH', None, 'UID', uid_range, *date_search)
        if status != 'OK':
            raise MailboxError('Nie udało się odczytać listy wiadomości. Spróbuj ponownie.')
        uids = sorted({_positive(int(uid)) for block in data or [] if block for uid in block.split()})
        if anchor is None:
            anchor = uids[-1] if uids else None
        else:
            uids = [uid for uid in uids if uid <= anchor]
        if excluded:
            excluded_uids = set(excluded(validity))
            uids = [uid for uid in uids if uid not in excluded_uids]
        candidates = uids
        newer = bool(cursor and cursor['direction'] == 'newer')
        if cursor:
            candidates = [uid for uid in uids if (uid > boundary if newer else uid < boundary)]
        selected = candidates[:PAGE_SIZE] if newer else candidates[-PAGE_SIZE:]
        result = {'validity': validity, 'rows': [], 'total': len(uids), 'next_cursor': None, 'previous_cursor': None}
        if not selected:
            # If this side of the cursor disappeared, return to newest remaining mail.
            selected = uids[-PAGE_SIZE:]
            if not selected:
                return result
        def state(direction, boundary):
            return {'anchor': anchor, 'validity': validity, 'direction': direction, 'boundary': boundary}
        if any(uid < selected[0] for uid in uids):
            result['next_cursor'] = state('older', selected[0])
        if any(uid > selected[-1] for uid in uids):
            result['previous_cursor'] = state('newer', selected[-1])
        status, payload = client.uid('FETCH', ','.join(map(str, selected)),
            '(UID BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT DATE MESSAGE-ID)]<0.65536>)')
        if status != 'OK':
            raise MailboxError('Nie udało się pobrać nagłówków. Spróbuj ponownie.')
        parser = BytesHeaderParser(policy=policy.default)
        for item in payload or []:
            if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[1], bytes):
                continue
            metadata, raw = item
            uid = re.search(rb'\bUID (\d+)\b', metadata)
            if not uid or int(uid[1]) not in selected:
                continue
            message = parser.parsebytes(raw)
            def field(name):
                return str(message.get(name, '')).replace('\r', ' ').replace('\n', ' ')[:2000]
            result['rows'].append({'uid': int(uid[1]), 'sender': field('Reply-To') or field('From'),
                'subject': field('Subject') if recruitment_roles is not None else story_title(field('Subject')), 'date': polish_date(field('Date')),
                'message_id': field('Message-ID')})
        result['rows'].sort(key=lambda item: item['uid'], reverse=True)
        return result
    except InvalidToken:
        raise MailboxError('Nie można odszyfrować hasła. Zapisz hasło skrzynki ponownie w panelu administratora.') from None
    except ssl.SSLError:
        raise MailboxError('Nie udało się nawiązać bezpiecznego połączenia TLS. Sprawdź serwer, port i certyfikat.') from None
    except (TimeoutError, OSError):
        raise MailboxError('Serwer pocztowy nie odpowiada lub połączenie zostało przerwane. Spróbuj ponownie.') from None
    except (imaplib.IMAP4.error, ValueError, UnicodeError, TypeError):
        raise MailboxError('Nie udało się odczytać danych IMAP. Sprawdź ustawienia skrzynki lub pobierz nagłówki ponownie.') from None
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def story_title(subject):
    return subject.partition('–')[2].strip() if '–' in subject else subject.strip()


def polish_date(value):
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo('Europe/Warsaw'))
        with override('pl'):
            return date_format(dt, 'j E Y, H:i')
    except (ValueError, TypeError, OverflowError):
        return '–'
