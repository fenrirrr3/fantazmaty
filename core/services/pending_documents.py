"""Private, short-lived uploads awaiting explicit rebuild consent."""
import hashlib
import os
import re
import secrets
import time
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile

from core.odkurzacz_forms import PROGRAM_MAX_UPLOAD_BYTES

TTL = 30 * 60
SALT = 'program-rebuild-upload-v1'
SESSION_KEY = 'program_upload_session'


class PendingDocumentError(ValueError):
    pass


def _root():
    return Path(getattr(settings, 'DOCUMENT_CONVERSION_DIR', settings.BASE_DIR / 'var' / 'conversion')) / 'pending_uploads'


def _cleanup():
    cutoff = time.time() - TTL
    for path in _root().glob('*/*.docx'):
        if not path.parent.name.isdecimal() or not re.fullmatch(r'[a-f0-9]{32}\.docx', path.name):
            continue
        try:
            if path.is_symlink() or path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def save_pending(request, upload, options, warning):
    _cleanup()
    upload.seek(0)
    payload = upload.read(PROGRAM_MAX_UPLOAD_BYTES + 1)
    upload.seek(0)
    if len(payload) > PROGRAM_MAX_UPLOAD_BYTES:
        raise PendingDocumentError('Plik przekracza limit 2 MB.')
    folder = _root() / str(request.user.pk)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = sorted(folder.glob('*.docx'), key=lambda p: p.stat().st_mtime)
    for old in existing[:-2]:
        old.unlink(missing_ok=True)
    key = secrets.token_hex(16)
    target = folder / (key + '.docx')
    try:
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            output.write(payload)
        nonce = request.session.get(SESSION_KEY) or secrets.token_hex(24)
        request.session[SESSION_KEY] = nonce
        return signing.dumps({'user': request.user.pk, 'session': nonce, 'key': key,
                              'name': upload.name, 'digest': hashlib.sha256(payload).hexdigest(),
                              'options': options, 'warning': warning}, salt=SALT, compress=True)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def load_pending(request, token):
    _cleanup()
    try:
        data = signing.loads(token, salt=SALT, max_age=TTL)
        if (data['user'] != request.user.pk or data['session'] != request.session.get(SESSION_KEY)
                or not re.fullmatch(r'[a-f0-9]{32}', data['key'])):
            raise ValueError
        path = _root() / str(request.user.pk) / (data['key'] + '.docx')
        if path.is_symlink():
            raise ValueError
        with path.open('rb') as source:
            payload = source.read(PROGRAM_MAX_UPLOAD_BYTES + 1)
        if len(payload) > PROGRAM_MAX_UPLOAD_BYTES or hashlib.sha256(payload).hexdigest() != data['digest']:
            raise ValueError
        return SimpleUploadedFile(data['name'], payload), data, path
    except (signing.BadSignature, ValueError, KeyError, TypeError, OSError):
        raise PendingDocumentError('Potwierdzenie wygasło, plik został już pobrany albo nie należy do tej sesji. Wybierz dokument ponownie.') from None
