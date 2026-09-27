"""Bounded read-only retrieval, field parsing and private download packaging."""
import hashlib
import imaplib
import json
import re
import ssl
import time
from io import BytesIO
from tempfile import SpooledTemporaryFile
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
from zipfile import ZipFile, ZIP_DEFLATED
from cryptography.fernet import InvalidToken
from django.core.files.uploadedfile import SimpleUploadedFile
from core.models import MailboxConnection, MailboxDownload
from core.forms import ReviewBulkImportForm
from core.odkurzacz_forms import OdkurzaczForm
from core.services.mailbox import MailboxError, encode_folder, story_title, _positive
from core.services.odkurzacz import clean_docx, ALL_EDITORIAL_RULES
from core.services.document_converter import convert_document
from texts.models import Anthology

MAX_MESSAGES = 10
MAX_MESSAGE = 15 * 1024 * 1024
MAX_BATCH = 50 * 1024 * 1024


def default_mailbox():
    boxes = list(MailboxConnection.objects.filter(is_active=True, name__iexact='teksty')[:2])
    if not boxes:
        boxes = list(MailboxConnection.objects.filter(is_active=True, username__istartswith='teksty@')[:2])
    if len(boxes) != 1:
        raise MailboxError('W adminie ustaw jedną aktywną skrzynkę o nazwie „teksty” (lub loginie teksty@…).')
    return boxes[0]


def mailbox_key(config):
    return hashlib.sha256(json.dumps([config.pk, config.host, config.port, config.security,
                                     config.username, config.folder]).encode()).hexdigest()


def receipts(config, validity):
    return MailboxDownload.objects.filter(mailbox_key=mailbox_key(config), uid_validity=validity)


def fetch_messages(config, validity, uids):
    uids = sorted(set(_positive(uid) for uid in uids))
    if not 1 <= len(uids) <= MAX_MESSAGES:
        raise MailboxError('Wybierz od 1 do 10 wiadomości.')
    client = None
    try:
        context = ssl.create_default_context()
        if config.security == 'ssl':
            client = imaplib.IMAP4_SSL(config.host, config.port, ssl_context=context, timeout=15)
        elif config.security == 'starttls':
            client = imaplib.IMAP4(config.host, config.port, timeout=15)
            client.starttls(ssl_context=context)
        else:
            raise MailboxError('Nieobsługiwany sposób szyfrowania.')
        client.login(config.username, config.get_password())
        folder = '"' + encode_folder(config.folder).replace('\\', '\\\\').replace('"', '\\"') + '"'
        if client.select(folder, readonly=True)[0] != 'OK':
            raise MailboxError('Nie można otworzyć folderu wiadomości.')
        _, data = client.response('UIDVALIDITY')
        if not data or int(data[0]) != validity:
            raise MailboxError('Folder pocztowy się zmienił. Pobierz nagłówki ponownie.')
        result, total = [], 0
        for uid in uids:
            status, data = client.uid('FETCH', str(uid), '(UID RFC822.SIZE)')
            meta = b' '.join(x for x in data or [] if isinstance(x, bytes))
            size = re.search(rb'RFC822.SIZE (\d+)', meta)
            if status != 'OK' or not size or int(size[1]) > MAX_MESSAGE:
                raise MailboxError(f'Wiadomość {uid} jest niedostępna lub przekracza 15 MB.')
            total += int(size[1])
            if total > MAX_BATCH:
                raise MailboxError('Wybrane wiadomości przekraczają łącznie 50 MB. Wybierz mniej pozycji.')
            status, data = client.uid('FETCH', str(uid), f'(UID BODY.PEEK[]<0.{MAX_MESSAGE + 1}>)')
            parts = [x for x in data or [] if isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], bytes)]
            if status != 'OK' or len(parts) != 1 or len(parts[0][1]) != int(size[1]):
                raise MailboxError(f'Nie udało się pobrać całej wiadomości {uid}.')
            found_uid = re.search(rb'\bUID (\d+)\b', parts[0][0])
            if not found_uid or int(found_uid[1]) != uid:
                raise MailboxError('Serwer zwrócił inną wiadomość. Pobierz nagłówki ponownie.')
            raw = parts[0][1]
            result.append(parse_message(uid, raw))
        return result
    except MailboxError:
        raise
    except (InvalidToken, imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError):
        raise MailboxError('Nie udało się pobrać wiadomości. Sprawdź połączenie i ustawienia skrzynki.') from None
    finally:
        if client:
            try: client.logout()
            except (imaplib.IMAP4.error, OSError): pass


def parse_message(uid, raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=('plain', 'html'))
    if body is None:
        raise MailboxError(f'Wiadomość {uid}: brak danych zgłoszenia w treści.')
    text = body.get_content()
    if body.get_content_type() == 'text/html':
        from lxml import html
        root = html.fromstring(text)
        for node in root.xpath('//br | //p | //div | //tr'):
            node.tail = '\n' + (node.tail or '')
        text = root.text_content()
    candidates = []
    for line in text.splitlines():
        parts = [value.strip() for value in line.split(';')[:7]]
        if len(parts) == 7 and '@' in parts[4] and ''.join(parts[3].split()).isdigit():
            candidates.append(parts)
    if len(candidates) != 1:
        raise MailboxError(f'Wiadomość {uid}: oczekiwano jednego wiersza autor;tytuł;gatunek;liczba znaków;e-mail;telefon;antologia.')
    author, title, genre, length, email, phone, anthology = candidates[0]
    files = []
    for part in message.walk():
        if part.is_multipart() or not part.get_filename():
            continue
        data = part.get_payload(decode=True)
        name = str(part.get_filename())
        if data is None:
            raise MailboxError(f'Wiadomość {uid}: nie można odczytać zawartości załącznika „{name}”. Sprawdź plik w poczcie lub poproś o ponowne przesłanie.')
        if not data:
            raise MailboxError(f'Wiadomość {uid}: załącznik „{name}” jest pusty (0 bajtów). Poproś nadawcę o ponowne przesłanie pliku. Zgłoszenie nie zostało zaimportowane.')
        files.append((name, data))
    if not files:
        raise MailboxError(f'Wiadomość {uid}: nie znaleziono załączników z plikami. Sprawdź wiadomość w poczcie.')
    if len(files) > 10:
        raise MailboxError(f'Wiadomość {uid}: znaleziono {len(files)} załączników; limit wynosi 10.')
    # Only these seven fields enter the importer. Never persist the mail body.
    return {'uid': uid, 'digest': hashlib.sha256(raw).hexdigest(),
            'folder': story_title(str(message.get('Subject', ''))) or title,
            'title': title, 'author': author, 'email': email, 'phone': phone,
            'anthology': anthology, 'genre': genre, 'length': length,
            'record': ';'.join((author, title, genre, length, '', email, phone)), 'files': files}


def prepare_forms(messages, config, validity, user, tokens=None, confirmed=False):
    previous = set(receipts(config, validity).values_list('uid', flat=True))
    grouped = {}
    for message in messages:
        message['downloaded'] = message['uid'] in previous
        if message['downloaded']:
            continue
        matches = list(Anthology.objects.filter(title__iexact=message['anthology'], status=Anthology.Status.IN_PREPARATION)[:2])
        if len(matches) != 1:
            raise MailboxError(f'Wiadomość {message["uid"]}: nie znaleziono jednoznacznej antologii „{message["anthology"]}” ze statusem W przygotowaniu.')
        pk = matches[0].pk
        grouped.setdefault(pk, []).append(message['record'])
    forms = []
    for pk, lines in sorted(grouped.items()):
        data = {'anthology': pk, 'records': '\n'.join(lines),
                'confirm_submission_warnings': confirmed,
                'submission_warnings_token': (tokens or {}).get(str(pk), '')}
        form = ReviewBulkImportForm(data, user=user)
        form.is_valid()
        forms.append(form)
    return forms


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:100].rstrip(' .')
    if not value or value.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(10)),*(f'LPT{i}' for i in range(10))}:
        value = 'opowiadanie_' + value
    return value


def package_messages(messages, clean=True, convert=True):
    archive_file = SpooledTemporaryFile(max_size=4*1024*1024, mode='w+b')
    used, total = set(), 0
    deadline = time.monotonic() + 90
    try:
        with ZipFile(archive_file, 'w', ZIP_DEFLATED) as archive:
            for message in messages:
                folder = safe_name(message['folder'])
                while folder.casefold() in used: folder += f'_{message["uid"]}'
                used.add(folder.casefold())
                for index, (name, data) in enumerate(message['files'], 1):
                    if time.monotonic() >= deadline:
                        raise MailboxError('Przetwarzanie paczki przekroczyło 90 sekund. Wybierz mniej wiadomości.')
                    name = safe_name(PurePosixPath(name.replace('\\', '/')).name)
                    stem = safe_name(PurePosixPath(name).stem)
                    base = f'{folder}/{index:02d}_{stem}'
                    is_docx = name.lower().endswith('.docx')
                    if (clean or convert) and not is_docx:
                        raise MailboxError(f'„{name}”: Odkurzacz i konwerter obsługują DOCX. Wyłącz obie opcje, aby pobrać oryginały.')
                    if is_docx:
                        form = OdkurzaczForm({'rules': []}, {'document': SimpleUploadedFile(name, data)})
                        if not form.is_valid():
                            raise MailboxError(f'Nieprawidłowy DOCX: {name}. ' + ' '.join(str(e) for errors in form.errors.values() for e in errors))
                    if clean:
                        with clean_docx(BytesIO(data), ALL_EDITORIAL_RULES) as output:
                            data = output.read()
                    archive.writestr(base + PurePosixPath(name).suffix, data)
                    total += len(data)
                    if convert:
                        upload = SimpleUploadedFile(name, data)
                        output, _, _ = convert_document(upload, ['pdf','epub'], use_cleaner=False, timeout=deadline-time.monotonic())
                        with output, ZipFile(output) as converted:
                            for ext in ('pdf', 'epub'):
                                payload = converted.read('document.' + ext)
                                total += len(payload)
                                if total > 150 * 1024 * 1024:
                                    raise MailboxError('Wynik przekracza 150 MB. Wybierz mniej wiadomości.')
                                archive.writestr(base + '.' + ext, payload)
        archive_file.seek(0)
        return archive_file
    except BaseException:
        archive_file.close()
        raise
