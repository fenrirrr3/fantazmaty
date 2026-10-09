"""Private file jobs for the three document tools; no database or task broker."""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.core.files import File

from core.odkurzacz_forms import PROGRAM_MAX_UPLOAD_BYTES
from .document_converter import (
    ConversionError, RebuildConfirmationRequired, convert_document,
    conversion_filename, converter_python,
)

TTL = 1800
SALT = 'private-program-job-v1'
SESSION_KEY = 'program_upload_session'


class JobError(ValueError):
    pass


class JobCancelled(Exception):
    pass


def root():
    return Path(getattr(settings, 'DOCUMENT_CONVERSION_DIR', settings.BASE_DIR / 'var' / 'conversion')) / 'program_jobs'


def write_json(path, value):
    # A unique temporary name per writer: the web process and the worker may
    # write the same state file concurrently.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False))
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.02)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.02)


def cleanup():
    cutoff = time.time() - TTL
    for folder in root().glob('*'):
        if not re.fullmatch(r'[a-f0-9]{32}', folder.name) or folder.is_symlink():
            continue
        try:
            if not folder.resolve().is_relative_to(root().resolve()):
                continue
            if (folder / 'request.json').stat().st_mtime < cutoff:
                state = read_json(folder / 'state.json')
                if state.get('state') in ('queued', 'running') and time.time() - state.get('started', 0) < 150:
                    continue
                shutil.rmtree(folder)
        except (OSError, ValueError):
            pass


def launch(folder):
    env = os.environ.copy()
    env['DJANGO_SETTINGS_MODULE'] = settings.SETTINGS_MODULE
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    kwargs = {'start_new_session': True} if os.name == 'posix' else {
        'creationflags': subprocess.CREATE_NO_WINDOW,
    }
    try:
        with (folder / 'runner.log').open('ab') as log:
            process = subprocess.Popen(
                [converter_python(), str(Path(__file__).with_name('program_job_worker.py')), str(folder.resolve())],
                cwd=settings.BASE_DIR, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, **kwargs,
            )
        # Reap only; document processing is never done in a WSGI thread.
        threading.Thread(target=process.wait, daemon=True).start()
        return process
    except (OSError, ConversionError):
        write_json(folder / 'state.json', {'state': 'error', 'message': 'Nie można uruchomić programu. Sprawdź konfigurację Pythona i log zadania.'})
        raise JobError('Nie można uruchomić programu. Skontaktuj się z administratorem.') from None


def max_active_jobs():
    """Server-wide cap: each worker loads python-docx/spaCy into its own process."""
    return max(1, int(getattr(settings, 'PROGRAM_MAX_ACTIVE_JOBS', 1)))


def active_jobs():
    """(user id, folder) for every queued or running job that is still alive."""
    result = []
    if not root().is_dir():
        return result
    for folder in root().iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            data = read_json(folder / 'request.json')
            state = read_json(folder / 'state.json')
        except (OSError, ValueError):
            continue
        if state.get('state') in ('queued', 'running') and time.time() - state.get('started', 0) < 150:
            result.append((data.get('user'), folder))
    return result


def require_capacity(user_id=None):
    active = active_jobs()
    if user_id is not None and any(owner == user_id for owner, _ in active):
        # Refuse accidental repeated clicks while this user's process is active.
        raise JobError('Masz już uruchomiony program. Poczekaj na wynik albo zatrzymaj go.')
    if len(active) >= max_active_jobs():
        raise JobError('Serwer przetwarza teraz maksymalną liczbę dokumentów. Spróbuj ponownie za minutę.')


def create(request, upload, options, action):
    cleanup()
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    require_capacity(request.user.pk)
    upload.seek(0)
    payload = upload.read(PROGRAM_MAX_UPLOAD_BYTES + 1)
    upload.seek(0)
    if len(payload) > PROGRAM_MAX_UPLOAD_BYTES:
        raise JobError('Plik przekracza limit 2 MB.')
    key = secrets.token_hex(16)
    folder = root() / key
    folder.mkdir(mode=0o700)
    nonce = request.session.get(SESSION_KEY) or secrets.token_hex(24)
    request.session[SESSION_KEY] = nonce
    with os.fdopen(os.open(folder / 'source.docx', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as output:
        output.write(payload)
    write_json(folder / 'request.json', {
        'user': request.user.pk, 'session': nonce, 'name': upload.name,
        'options': options, 'action': action,
    })
    write_json(folder / 'state.json', {'state': 'queued', 'stage': 'Uruchamianie programu', 'started': time.time()})
    launch(folder)
    return signing.dumps({'key': key, 'user': request.user.pk, 'session': nonce}, salt=SALT)


def resolve(request, token):
    try:
        value = signing.loads(token, salt=SALT, max_age=TTL)
        if value['user'] != request.user.pk or value['session'] != request.session.get(SESSION_KEY):
            raise ValueError()
        if not re.fullmatch(r'[a-f0-9]{32}', value['key']):
            raise ValueError()
        folder = root() / value['key']
        if folder.is_symlink():
            raise ValueError()
        data = read_json(folder / 'request.json')
        if data['user'] != value['user'] or data['session'] != value['session']:
            raise ValueError()
        return folder
    except (signing.BadSignature, ValueError, KeyError, OSError):
        raise JobError('Zadanie wygasło lub nie jest dostępne w tej sesji. Wybierz plik ponownie.') from None


def status(folder):
    state = read_json(folder / 'state.json')
    if (folder / 'cancel').exists() and state['state'] == 'confirmation':
        return {'state': 'cancelled', 'message': 'Praca zatrzymana.'}
    if state['state'] in ('queued', 'running') and time.time() - state.get('started', 0) > 150:
        return {'state': 'error', 'message': 'Proces nie odpowiedział w przewidzianym czasie. Sprawdź log zadania na serwerze.'}
    return state


def confirm(folder):
    if status(folder)['state'] != 'confirmation' or (folder / 'cancel').exists():
        raise JobError('To zadanie nie oczekuje na potwierdzenie.')
    try:
        with (folder / 'accepted').open('x'):
            pass
    except FileExistsError:
        raise JobError('Potwierdzenie zostało już wysłane.') from None
    try:
        require_capacity()
    except JobError:
        (folder / 'accepted').unlink(missing_ok=True)
        raise
    os.utime(folder / 'request.json', None)
    write_json(folder / 'state.json', {'state': 'queued', 'stage': 'Wznawianie po akceptacji', 'started': time.time()})
    try:
        launch(folder)
    except JobError:
        (folder / 'accepted').unlink(missing_ok=True)
        raise


def refreshed_token(token):
    return signing.dumps(signing.loads(token, salt=SALT, max_age=TTL), salt=SALT)


def execute(folder):
    data = read_json(folder / 'request.json')
    started = time.time()
    last_progress = None

    def control(directory):
        nonlocal last_progress
        if (folder / 'cancel').exists():
            raise JobCancelled()
        try:
            progress = read_json(directory / 'progress.json')
        except (OSError, ValueError):
            return
        if progress.get('stage') == 'start':
            progress = {'stage': 'Uruchamianie konwertera'}
        if progress != last_progress:
            last_progress = progress
            write_json(folder / 'state.json', {'state': 'running', 'started': started, **progress})

    write_json(folder / 'state.json', {'state': 'running', 'stage': 'Sprawdzanie dokumentu', 'started': started})
    try:
        if (folder / 'cancel').exists():
            raise JobCancelled()
        options = dict(data['options'])
        if (folder / 'accepted').exists():
            options['allow_rebuild_omissions'] = True
        with (folder / 'source.docx').open('rb') as source:
            result, extension, mime = convert_document(File(source, name=data['name']), control=control, **options)
        with result, (folder / 'result').open('wb') as target:
            shutil.copyfileobj(result, target)
        if (folder / 'cancel').exists():
            raise JobCancelled()
        name = data['name']
        if data['action'] != 'convert':
            suffix = '_powtorzenia' if data['action'] == 'repetitions' else ('_nowy' if options.get('rebuild') else '_odkurzony')
            name = Path(name).stem[:120] + suffix + '.docx'
        write_json(folder / 'state.json', {'state': 'done', 'filename': conversion_filename(name, extension), 'mime': mime,
                                          'warnings': len(getattr(result, 'conversion_warnings', []))})
    except RebuildConfirmationRequired as error:
        write_json(folder / 'state.json', {'state': 'confirmation', 'message': str(error)})
        return
    except JobCancelled:
        (folder / 'result').unlink(missing_ok=True)
        write_json(folder / 'state.json', {'state': 'cancelled', 'message': 'Praca zatrzymana.'})
    except Exception as error:
        import logging
        logging.getLogger(__name__).exception('Błąd zadania programu')
        message = str(error) if isinstance(error, ConversionError) else 'Nie udało się przetworzyć dokumentu. Szczegóły zapisano w logu zadania.'
        write_json(folder / 'state.json', {'state': 'error', 'message': message})
    (folder / 'source.docx').unlink(missing_ok=True)
