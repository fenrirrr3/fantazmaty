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
from django.db.models import Q
from core.models import MailboxConnection, MailboxDownload
from core.forms import ReviewBulkImportForm
from core.odkurzacz_forms import OdkurzaczForm
from core.services.mailbox import MailboxError, encode_folder, story_title, _positive
from core.services.document_converter import inspect_document, convert_document, RebuildConfirmationRequired
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
            metadata = parse_message(uid, raw, submission=False)
            previous = receipts(config, validity).filter(uid=uid).exists() or MailboxDownload.objects.filter(fingerprint=metadata['fingerprint']).exists()
            result.append(metadata if previous else parse_message(uid, raw))
        return result
    except MailboxError:
        raise
    except (InvalidToken, imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError):
        raise MailboxError('Nie udało się pobrać wiadomości. Sprawdź połączenie i ustawienia skrzynki.') from None
    finally:
        if client:
            try: client.logout()
            except (imaplib.IMAP4.error, OSError): pass


def parse_message(uid, raw, *, submission=True):
    message = BytesParser(policy=policy.default).parsebytes(raw)
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
    # A stable identity independent of IMAP folder, UIDVALIDITY and local settings.
    # Include decoded contents to avoid trusting a duplicated Message-ID alone.
    identity = hashlib.sha256()
    message_id = str(message.get('Message-ID', '')).strip()[:998]
    for value in (message_id, str(message.get('From', '')), str(message.get('Subject', ''))):
        encoded = value.encode('utf-8')
        identity.update(len(encoded).to_bytes(8, 'big')); identity.update(encoded)
    for part in message.walk():
        if part.is_multipart():
            continue
        for value in (part.get_content_type().encode(), str(part.get_filename() or '').encode('utf-8'), part.get_payload(decode=True) or b''):
            identity.update(len(value).to_bytes(8, 'big')); identity.update(value)
    metadata = {'uid': uid, 'digest': hashlib.sha256(raw).hexdigest(),
                'fingerprint': identity.hexdigest(), 'message_id': message_id,
                'folder': story_title(str(message.get('Subject', ''))) or f'wiadomosc_{uid}',
                'title': story_title(str(message.get('Subject', ''))) or '(bez tytułu)',
                'files': files}
    if not submission:
        return metadata
    body = message.get_body(preferencelist=('plain', 'html'))
    if body is None:
        raise MailboxError(f'Wiadomość {uid}: brak danych zgłoszenia w treści.')
    text = body.get_content()
    if body.get_content_type() == 'text/html':
        from lxml import html
        from lxml.etree import ParserError, XMLSyntaxError
        try:
            root = html.fromstring(text)
        except (ParserError, XMLSyntaxError, ValueError):
            raise MailboxError(f'Wiadomość {uid}: treść HTML jest pusta lub nie można jej odczytać. Użyj ręcznego importu z podglądem albo poproś o ponowne przesłanie danych zgłoszenia. Niczego nie zaimportowano.') from None
        for node in root.xpath('//br | //p | //div | //tr'):
            node.tail = '\n' + (node.tail or '')
        text = root.text_content()
    from core.services.review_import_parser import unbracket, encode_submission, clean_pasted_submission
    candidates = []
    lines = clean_pasted_submission(text).splitlines()
    for index, line in enumerate(lines):
        parts = [unbracket(value.strip()) for value in line.split(';', 8)]
        if len(parts) == 9 and '@' in parts[5] and ''.join(parts[4].split()).isdigit():
            candidates.append(('new', index, parts))
        elif len(parts) >= 7 and '@' in parts[4] and ''.join(parts[3].split()).isdigit():
            candidates.append(('old', index, parts))
    if len(candidates) != 1:
        raise MailboxError(f'Wiadomość {uid}: oczekiwano jednego wiersza autor;tytuł;gatunek;content warningi;liczba znaków;e-mail;telefon;zgody;wiadomość.')
    kind, index, parts = candidates[0]
    if kind == 'new':
        author, title, genre, warnings, length, email, phone, choices, author_message = parts
        trailing = lines[index + 1:]
        marker = '--- KONIEC WIADOMOŚCI AUTORA ---'
        if any(line.strip() == marker for line in trailing):
            stop = next(i for i, line in enumerate(trailing) if line.strip() == marker)
            author_message = unbracket('\n'.join([author_message, *trailing[:stop]]).strip())
        else:
            # Marker is optional; without it all remaining lines belong to the author message.
            author_message = unbracket('\n'.join([author_message, *trailing]).strip())
        recruitment = re.search(r'Nabór:\s*[„"]([^”"]+)[”"]', str(message.get('Subject', '')), re.IGNORECASE)
        if not recruitment:
            raise MailboxError(f'Wiadomość {uid}: w temacie brakuje nazwy antologii w formacie Nabór: „Tytuł antologii”.')
        anthology = recruitment.group(1).strip()
        record = encode_submission([author, title, genre, warnings, length, email, phone, choices, author_message])
    else:
        author, title, genre, length, email, phone, anthology = parts[:7]
        if len(parts) >= 8:
            from core.services.newsletters import parse_consents
            from django.core.exceptions import ValidationError
            try:
                parse_consents(parts[7])
            except ValidationError as error:
                raise MailboxError(f'Wiadomość {uid}: nieprawidłowe zgody newsletterowe. ' + ' '.join(error.messages)) from None
            else:
                record = encode_submission([author, title, genre, '', length, email, phone, parts[7], ''])
        else:
            record = encode_submission([author, title, genre, length, '', email, phone])
    # Persist only the declared submission fields, not the full MIME message.
    return {**metadata, 'uid': uid, 'digest': hashlib.sha256(raw).hexdigest(),
            'folder': story_title(str(message.get('Subject', ''))) or title,
            'title': title, 'author': author, 'email': email, 'phone': phone,
            'anthology': anthology, 'genre': genre, 'length': length,
            'record': record, 'files': files}


def prepare_forms(messages, config, validity, user, tokens=None, confirmed=False):
    previous = set(receipts(config, validity).values_list('uid', flat=True))
    fingerprints = set(MailboxDownload.objects.filter(fingerprint__in=[m.get('fingerprint', '') for m in messages if m.get('fingerprint')]).values_list('fingerprint', flat=True))
    grouped = {}
    for message in messages:
        message['downloaded'] = message['uid'] in previous or bool(message.get('fingerprint') and message['fingerprint'] in fingerprints)
        if message['downloaded']:
            continue
        if message.get('fingerprint'):
            fingerprints.add(message['fingerprint'])
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


def package_messages(messages, clean=True, convert=True, rebuild=True, allow_rebuild_omissions=False):
    archive_file = SpooledTemporaryFile(max_size=4*1024*1024, mode='w+b')
    used, total = set(), 0
    rebuild_warnings = []
    deadline = time.monotonic() + 90
    try:
        if rebuild and not allow_rebuild_omissions:
            for message in messages:
                for name, data in message['files']:
                    form = OdkurzaczForm({'rules': []}, {'document': SimpleUploadedFile(name, data)})
                    if not form.is_valid():
                        raise MailboxError(f'Nieprawidłowy DOCX: {name}. Wyłącz przetwarzanie, aby pobrać oryginał.')
                    try:
                        inspect_document(SimpleUploadedFile(name, data), timeout=deadline-time.monotonic())
                    except RebuildConfirmationRequired as error:
                        rebuild_warnings.append(f"{name}: {error}")
            if rebuild_warnings:
                raise RebuildConfirmationRequired(' '.join(rebuild_warnings))
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
                    if (clean or convert or rebuild) and not is_docx:
                        raise MailboxError(f'„{name}”: Odkurzacz i konwerter obsługują DOCX. Wyłącz Odkurzacz, konwersję i przebudowę, aby pobrać oryginały.')
                    if is_docx and (clean or convert or rebuild):
                        form = OdkurzaczForm({'rules': []}, {'document': SimpleUploadedFile(name, data)})
                        if not form.is_valid():
                            raise MailboxError(f'Nieprawidłowy DOCX: {name}. ' + ' '.join(str(e) for errors in form.errors.values() for e in errors))
                    if not convert and not rebuild:
                        if clean:
                            output, _, _ = convert_document(SimpleUploadedFile(name, data), [], include_docx=True, use_cleaner=True, normalize=False, timeout=deadline-time.monotonic())
                            with output:
                                data = output.read()
                        archive.writestr(base + PurePosixPath(name).suffix, data)
                        total += len(data)
                    if convert or rebuild:
                        upload = SimpleUploadedFile(name, data)
                        try:
                            output, _, _ = convert_document(upload, ['pdf','epub'] if convert else [], use_cleaner=clean, timeout=deadline-time.monotonic(), include_docx=True, rebuild=rebuild, normalize=convert, justify=convert, allow_rebuild_omissions=allow_rebuild_omissions)
                        except RebuildConfirmationRequired as error:
                            rebuild_warnings.append(f"{name}: {error}")
                            continue
                        if not convert:
                            with output:
                                payload = output.read()
                            archive.writestr(base + '.docx', payload)
                            total += len(payload)
                            if total > 150 * 1024 * 1024:
                                raise MailboxError('Wynik przekracza 150 MB. Wybierz mniej wiadomości.')
                            continue
                        with output, ZipFile(output) as converted:
                            for ext in ('docx', 'pdf', 'epub'):
                                payload = converted.read('document.' + ext)
                                total += len(payload)
                                if total > 150 * 1024 * 1024:
                                    raise MailboxError('Wynik przekracza 150 MB. Wybierz mniej wiadomości.')
                                archive.writestr(base + '.' + ext, payload)
        if rebuild_warnings:
            raise RebuildConfirmationRequired(' '.join(rebuild_warnings))
        if time.monotonic() > deadline:
            raise MailboxError('Przygotowanie paczki przekroczyło 90 sekund. Wybierz mniej wiadomości.')
        archive_file.seek(0)
        return archive_file
    except BaseException:
        archive_file.close()
        raise
