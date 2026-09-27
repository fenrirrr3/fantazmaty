"""Read-only IMAP headers with stable UID cursors and modified UTF-7 folders."""
import base64
import imaplib
import re
import ssl
from email import policy
from email.parser import BytesHeaderParser
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


def read_headers(config, cursor=None):
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
        status, data = client.uid('SEARCH', None, 'UID', f'1:{anchor}' if anchor else '1:*')
        if status != 'OK':
            raise MailboxError('Nie udało się odczytać listy wiadomości. Spróbuj ponownie.')
        uids = sorted({_positive(int(uid)) for block in data or [] if block for uid in block.split()})
        if anchor is None:
            anchor = uids[-1] if uids else None
        else:
            uids = [uid for uid in uids if uid <= anchor]
        candidates = uids
        newer = bool(cursor and cursor['direction'] == 'newer')
        if cursor:
            candidates = [uid for uid in uids if (uid > boundary if newer else uid < boundary)]
        selected = candidates[:PAGE_SIZE] if newer else candidates[-PAGE_SIZE:]
        result = {'rows': [], 'total': len(uids), 'next_cursor': None, 'previous_cursor': None}
        if not selected:
            # Messages may have disappeared between pages; offer a fresh read.
            return result
        def state(direction, boundary):
            return {'anchor': anchor, 'validity': validity, 'direction': direction, 'boundary': boundary}
        if any(uid < selected[0] for uid in uids):
            result['next_cursor'] = state('older', selected[0])
        if any(uid > selected[-1] for uid in uids):
            result['previous_cursor'] = state('newer', selected[-1])
        status, payload = client.uid('FETCH', ','.join(map(str, selected)),
            '(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]<0.65536>)')
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
            result['rows'].append({'uid': int(uid[1]), 'sender': field('From'),
                'recipient': field('To'), 'subject': field('Subject'), 'date': field('Date'),
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
