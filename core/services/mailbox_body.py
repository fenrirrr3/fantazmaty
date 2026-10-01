"""Read only a bounded text part of an IMAP message, never its attachments."""
import re
from core.services.mailbox import MailboxError

MAX_BODY = 1024 * 1024
TOKEN = re.compile(rb'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+)')


def _structure(value):
    tokens = [m.group(1) for m in TOKEN.finditer(value)]
    cursor = 0
    def read(depth=0):
        nonlocal cursor
        if depth > 30 or cursor >= len(tokens):
            raise MailboxError('Niepoprawna struktura MIME wiadomości.')
        token = tokens[cursor]; cursor += 1
        if token == b'(':
            result = []
            while cursor < len(tokens) and tokens[cursor] != b')':
                result.append(read(depth + 1))
            if cursor >= len(tokens): raise MailboxError('Niepełna struktura MIME.')
            cursor += 1
            return result
        if token.startswith(b'"'):
            return re.sub(rb'\\(.)', rb'\1', token[1:-1]).decode('ascii', 'replace')
        if token.upper() == b'NIL': return None
        return int(token) if token.isdigit() else token.decode('ascii', 'replace')
    return read()


def _parts(structure, prefix=''):
    if not isinstance(structure, list) or not structure:
        return
    if isinstance(structure[0], list):
        for number, child in enumerate(structure, 1):
            if not isinstance(child, list): break
            yield from _parts(child, f'{prefix}.{number}' if prefix else str(number))
    elif len(structure) >= 7 and str(structure[0]).lower() == 'text' and str(structure[1]).lower() in ('plain', 'html'):
        params = structure[2] or []
        # Named text attachments do not represent the submission body.
        if isinstance(params, list) and any(str(v).lower() == 'name' for v in params[::2]): return
        disposition = structure[9] if len(structure) > 9 else None
        if isinstance(disposition, list) and str(disposition[0]).lower() == 'attachment': return
        yield prefix or '1', structure


def fetch_text_body(client, uid):
    status, data = client.uid('FETCH', str(uid), '(UID BODYSTRUCTURE)')
    meta = b' '.join(item for item in data or [] if isinstance(item, bytes))
    identity = re.search(rb'\bUID (\d+)\b', meta)
    marker = re.search(rb'\bBODYSTRUCTURE\s+', meta, re.I)
    if status != 'OK' or not identity or int(identity[1]) != uid or not marker or len(meta) > 256 * 1024:
        raise MailboxError('Nie udało się odczytać struktury wiadomości. Ponów partię.')
    candidates = list(_parts(_structure(meta[marker.end():])))
    candidates.sort(key=lambda item: str(item[1][1]).lower() != 'plain')
    if not candidates: return None
    section, part = candidates[0]
    if not isinstance(part[6], int) or part[6] > MAX_BODY: return None
    status, data = client.uid('FETCH', str(uid), f'(UID BODY.PEEK[{section}]<0.{MAX_BODY + 1}>)')
    literals = [item for item in data or [] if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes)]
    if status != 'OK' or len(literals) != 1:
        raise MailboxError('Nie udało się pobrać treści wiadomości. Ponów partię.')
    meta, content = literals[0]
    identity = re.search(rb'\bUID (\d+)\b', meta)
    if not identity or int(identity[1]) != uid or len(content) != part[6] or len(content) > MAX_BODY:
        raise MailboxError('Serwer zwrócił niepełną lub inną wiadomość. Ponów partię.')
    params = part[2] or []
    charset = next((str(params[n + 1]) for n in range(0, len(params) - 1, 2) if str(params[n]).lower() == 'charset'), 'utf-8')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,60}', charset): charset = 'utf-8'
    encoding = str(part[5]).lower()
    if encoding not in ('7bit', '8bit', 'binary', 'base64', 'quoted-printable'):
        raise MailboxError('Nieobsługiwane kodowanie wiadomości.')
    headers = f'Content-Type: text/{str(part[1]).lower()}; charset="{charset}"\r\nContent-Transfer-Encoding: {encoding}\r\n\r\n'
    return headers.encode('ascii') + content
