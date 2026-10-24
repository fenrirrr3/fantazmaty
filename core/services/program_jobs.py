"""Prywatne zadania programów dokumentów (pliki na dysku, bez bazy i brokera zadań).

Odkurzacz, konwerter i kolorowanie powtórzeń przetwarzają jeden plik. Scalanie
(„merge”) przebudowuje i odkurza każdy plik osobno, a potem skleja wyniki;
podział („split”) tnie jeden plik według Nagłówków 1 w izolowanym podprocesie.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.core.files import File

from core.odkurzacz_forms import MERGE_MAX_FILES, MERGE_MAX_TOTAL_BYTES, PROGRAM_MAX_UPLOAD_BYTES, plural
from core.services.odkurzacz import EDITORIAL_RULES
from .document_converter import (
    MAX_OUTPUT, REBUILD_WARNING, ConversionError, RebuildConfirmationRequired, _try_lock, conversion_slot,
    convert_document, conversion_filename, converter_python, run_converter, validate_resources,
)
from .document_images import linked_images_note

TTL = 1800
STALE_AFTER = 150      # brak znaku życia procesu przez tyle sekund = błąd
QUEUE_WAIT = 600       # najkrótszy limit oczekiwania w kolejce na wolny konwerter (patrz queue_wait)
QUEUE_MARGIN = 30      # zapas na każde zadanie przed nami w kolejce (start podprocesu, kopiowanie wyniku)
SALT = 'private-program-job-v1'
SESSION_KEY = 'program_upload_session'
# Zadania superusera (analiza bez limitu wielkości) mogą trwać dłużej niż 90 s.
SUPERUSER_TIME_LIMIT = 300
# Koniec komunikatu o pominięciach przy przebudowie (document_converter.run_converter).
REBUILD_SUFFIX = '. Po akceptacji powstanie nowy DOCX bez tych elementów.'
MERGE_REBUILD_SUFFIX = ('. Po akceptacji wszystkie pliki zostaną przebudowane bez tych elementów i scalone. '
                        'Tekst i obsługiwane formatowanie zostaną przeniesione.')
NO_HEADING_MESSAGE = ('Dokument nie zawiera akapitów ze stylem „Nagłówek 1”. Oznacz tytuły opowiadań '
                      'stylem Nagłówek 1 (Narzędzia główne → Style) i spróbuj ponownie.')
DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
# Lista części podziału (plik w ZIP i nagłówek) zapisana obok wyniku – korzysta z niej wysyłka autorom.
SPLIT_PARTS = 'split_parts.json'
SUMMARY_LABELS = {'user_word_corrections': 'Własne zamiany słownikowe (słownik zamian)',
                  'nonbreaking_spaces': 'Spacje nierozdzielające i nietypowe zamienione na zwykłe'}


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


def alive(state):
    """Zadanie w kolejce lub w toku, którego proces ostatnio dał znak życia."""
    last = state.get('heartbeat', state.get('started', 0))
    return state.get('state') in ('queued', 'running') and time.time() - last < STALE_AFTER


def cleanup():
    cutoff = time.time() - TTL
    for folder in root().glob('*'):
        if not re.fullmatch(r'[a-f0-9]{32}', folder.name) or folder.is_symlink():
            continue
        try:
            if not folder.resolve().is_relative_to(root().resolve()):
                continue
            request_file = folder / 'request.json'
            # Folder bez request.json (przerwane tworzenie) liczymy od daty folderu.
            stamp = request_file.stat().st_mtime if request_file.exists() else folder.stat().st_mtime
            if stamp < cutoff:
                try:
                    if alive(read_json(folder / 'state.json')):
                        continue
                except (OSError, ValueError):
                    pass
                shutil.rmtree(folder)
        except (OSError, ValueError):
            pass
    # Katalogi robocze konwertera zostają po przerwanym procesie (np. restart workera).
    conversion = root().parent
    for folder in conversion.glob('document-*'):
        try:
            if folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder)
        except OSError:
            pass


@contextmanager
def creation_lock(wait=5):
    """Jedno tworzenie zadania naraz – limit kolejki działa też przy podwójnym kliknięciu."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    # Plik blokady obok katalogu zadań: w środku są wyłącznie foldery zadań.
    with (root().parent / 'program_jobs.lock').open('a+b') as lock:
        deadline = time.monotonic() + wait
        while not _try_lock(lock):
            if time.monotonic() >= deadline:
                raise JobError('Serwer przyjmuje teraz inne zadanie. Spróbuj ponownie za chwilę.')
            time.sleep(0.1)
        try:
            yield
        finally:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


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


def max_queued_jobs():
    """Server-wide cap of waiting and running jobs.

    Dokumenty są przetwarzane po kolei (conversion_slot); pozostałe czekają
    w kolejce. Każde zadanie w kolejce to lekki proces nadzorcy.
    """
    return max(1, int(getattr(settings, 'PROGRAM_MAX_QUEUED_JOBS', 5)))


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
        if alive(state):
            result.append((data.get('user'), folder))
    return result


def require_capacity(user_id=None):
    active = active_jobs()
    if user_id is not None and any(owner == user_id for owner, _ in active):
        # Refuse accidental repeated clicks while this user's process is active.
        raise JobError('Masz już uruchomiony program. Poczekaj na wynik albo zatrzymaj go.')
    if len(active) >= max_queued_jobs():
        raise JobError('Kolejka programów jest pełna. Spróbuj ponownie za kilka minut.')


def create(request, upload, options, action):
    """Zadanie dla jednego pliku albo – przy scalaniu – listy plików (kolejność = kolejność scalania)."""
    cleanup()
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    with creation_lock():
        return _create(request, upload, options, action)


def _payload(upload):
    upload.seek(0)
    payload = upload.read(PROGRAM_MAX_UPLOAD_BYTES + 1)
    upload.seek(0)
    if len(payload) > PROGRAM_MAX_UPLOAD_BYTES:
        raise JobError(f'Plik przekracza limit {PROGRAM_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.')
    return payload


def _write_private(path, payload):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as output:
        output.write(payload)


def _create(request, upload, options, action):
    require_capacity(request.user.pk)
    uploads = list(upload) if isinstance(upload, (list, tuple)) else [upload]
    if not uploads or len(uploads) > MERGE_MAX_FILES:
        raise JobError(f'Wybierz od 1 do {MERGE_MAX_FILES} plików.')
    payloads = [_payload(item) for item in uploads]
    if sum(map(len, payloads)) > MERGE_MAX_TOTAL_BYTES:
        raise JobError(f'Pliki przekraczają łączny limit {MERGE_MAX_TOTAL_BYTES // (1024 * 1024)} MB.')
    key = secrets.token_hex(16)
    folder = root() / key
    folder.mkdir(mode=0o700)
    nonce = request.session.get(SESSION_KEY) or secrets.token_hex(24)
    request.session[SESSION_KEY] = nonce
    try:
        names = [Path(str(item.name).replace('\\', '/')).name for item in uploads]
        if action == 'merge':
            (folder / 'sources').mkdir(mode=0o700)
            for index, payload in enumerate(payloads, 1):
                _write_private(folder / 'sources' / f'{index:02d}.docx', payload)
        else:
            _write_private(folder / 'source.docx', payloads[0])
        write_json(folder / 'request.json', {
            'user': request.user.pk, 'session': nonce, 'name': uploads[0].name,
            'options': options, 'action': action, **({'names': names} if action == 'merge' else {}),
        })
        write_json(folder / 'state.json', {'state': 'queued', 'stage': 'Uruchamianie programu', 'started': time.time()})
        launch(folder)
    except BaseException:
        # Użytkownik nie dostanie tokenu, więc kopia dokumentu nie może czekać na sprzątanie.
        remove(folder)
        raise
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
    if not isinstance(state.get('warnings', []), list):
        state['warnings'] = []  # dawny zapis: sama liczba uwag
    if 'summary' in state and not isinstance(state['summary'], list):
        state['summary'] = []
    if 'replacements' in state and not isinstance(state['replacements'], list):
        state['replacements'] = []
    if 'removed_images' in state and (not isinstance(state['removed_images'], int)
                                      or isinstance(state['removed_images'], bool)):
        state['removed_images'] = 0
    if (folder / 'cancel').exists() and state['state'] == 'confirmation':
        return {'state': 'cancelled', 'message': 'Praca zatrzymana.'}
    if state['state'] in ('queued', 'running') and not alive(state):
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


def remove(folder):
    """Usuń zadanie razem z wynikiem (po pobraniu pliku)."""
    if re.fullmatch(r'[a-f0-9]{32}', folder.name) and folder.resolve().is_relative_to(root().resolve()):
        shutil.rmtree(folder, ignore_errors=True)


def superuser_time_limit():
    from .document_converter import MAX_TIME_LIMIT, TIME_LIMIT
    value = int(getattr(settings, 'PROGRAM_SUPERUSER_TIME_LIMIT', SUPERUSER_TIME_LIMIT))
    return max(TIME_LIMIT, min(MAX_TIME_LIMIT, value))


def summary_rows(summary):
    """Podsumowanie Odkurzacza {reguła: liczba} jako wiersze w kolejności reguł formularza."""
    if summary is None:
        return None
    labels = dict(EDITORIAL_RULES) | SUMMARY_LABELS
    order = {key: index for index, (key, _) in enumerate(EDITORIAL_RULES)}
    # Lista zamian słownikowych („replacements”) nie jest wierszem reguły – patrz replacement_rows.
    rows = sorted(((key, count) for key, count in summary.items()
                   if isinstance(count, int) and not isinstance(count, bool) and count > 0),
                  key=lambda item: (order.get(item[0], len(order)), item[0]))
    return [{'rule': key, 'label': labels.get(key, key), 'count': count} for key, count in rows[:60]]


def replacement_rows(summary):
    """Zamiany słownikowe z podsumowania: [{source, target, count}] (co na co, ile razy)."""
    rows = (summary or {}).get('replacements')
    if not isinstance(rows, list):
        return []
    return [{'source': row['source'], 'target': row['target'], 'count': row['count']} for row in rows[:200]
            if isinstance(row, dict) and isinstance(row.get('source'), str) and isinstance(row.get('target'), str)
            and isinstance(row.get('count'), int) and not isinstance(row.get('count'), bool) and row['count'] > 0]


def job_time_limit(options):
    """Limit czasu jednego zadania w konwerterze (sekundy)."""
    from .document_converter import MAX_TIME_LIMIT, TIME_LIMIT
    value = options.get('timeout', TIME_LIMIT) if isinstance(options, dict) else TIME_LIMIT
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = TIME_LIMIT
    return max(1, min(MAX_TIME_LIMIT, value))


def queue_wait(folder):
    """Najdłuższe oczekiwanie na konwerter: co najmniej QUEUE_WAIT, a gdy w kolejce stoją inne zadania –
    suma ich limitów czasu z zapasem (zadanie superusera może zająć konwerter na MAX_TIME_LIMIT)."""
    total = 0
    for _, other in active_jobs():
        if other.name == folder.name:
            continue
        try:
            total += job_time_limit(read_json(other / 'request.json').get('options')) + QUEUE_MARGIN
        except (OSError, ValueError, AttributeError):
            total += job_time_limit(None) + QUEUE_MARGIN
    return max(QUEUE_WAIT, total)


def refreshed_token(token):
    return signing.dumps(signing.loads(token, salt=SALT, max_age=TTL), salt=SALT)


def _sum_summaries(summaries):
    """Podsumowania Odkurzacza z kilku plików: liczby reguł i zamian słownikowych zsumowane."""
    total, replacements = {}, {}
    for summary in summaries:
        for key, count in (summary or {}).items():
            if isinstance(count, int) and not isinstance(count, bool):
                total[key] = total.get(key, 0) + count
        for row in replacement_rows(summary):
            pair = (row['source'], row['target'])
            replacements[pair] = replacements.get(pair, 0) + row['count']
    if replacements:
        total['replacements'] = sorted(({'source': source, 'target': target, 'count': count}
                                        for (source, target), count in replacements.items()),
                                       key=lambda row: -row['count'])
    return total


def _rebuild_omissions(error):
    """Same pominięcia z komunikatu RebuildConfirmationRequired (bez wstępu i zakończenia)."""
    message = str(error)
    if message.startswith(REBUILD_WARNING):
        message = message[len(REBUILD_WARNING):]
    return message.split(REBUILD_SUFFIX, 1)[0].strip()


def _run_single(folder, data, options, *, control, waiting):
    with (folder / 'source.docx').open('rb') as source:
        result, extension, mime = convert_document(File(source, name=data['name']), control=control,
                                                   wait_for_slot=queue_wait(folder), on_wait=waiting, **options)
    with result, (folder / 'result').open('wb') as target:
        shutil.copyfileobj(result, target)
    name = data['name']
    if data['action'] != 'convert':
        suffix = '_powtorzenia' if data['action'] == 'repetitions' else ('_nowy' if options.get('rebuild') else '_odkurzony')
        name = Path(name).stem[:120] + suffix + '.docx'
    done = {'state': 'done', 'filename': conversion_filename(name, extension), 'mime': mime,
            'warnings': list(getattr(result, 'conversion_warnings', []))[:30]}
    # Liczba obrazów usuniętych z dokumentu przed pracą programu (strona stanu pokazuje komunikat).
    removed = getattr(result, 'removed_images', 0)
    done['removed_images'] = removed if isinstance(removed, int) and not isinstance(removed, bool) else 0
    if options.get('cleaner_summary'):
        summary = getattr(result, 'cleaner_summary', None) or {}
        done['summary'] = summary_rows(summary)
        done['replacements'] = replacement_rows(summary)
    return done


def _run_merge(folder, data, options, *, control, waiting):
    """Każdy plik przebudowany i odkurzony osobno (jak Odkurzacz z przebudową), potem jeden DOCX.

    Gotowe części zostają w folderze zadania: po akceptacji pominięć przetwarzamy
    ponownie tylko pliki, które jej wymagały.
    """
    from .document_merge import merge_documents
    from .document_template import apply_template_styles

    # Style szablonu dotyczą tylko scalonego wyniku – konwerter tej opcji nie zna.
    use_template = options.pop('use_template', False)
    names = data['names']
    parts = folder / 'parts'
    parts.mkdir(mode=0o700, exist_ok=True)
    warnings, omissions, summaries, removed = [], [], [], 0
    for index, name in enumerate(names, 1):
        part = parts / f'{index:02d}.docx'
        report = parts / f'{index:02d}.json'
        if not part.exists():
            prefix = f'Plik {index} z {len(names)}: '
            try:
                with (folder / 'sources' / f'{index:02d}.docx').open('rb') as source:
                    result, _, _ = convert_document(
                        File(source, name=name), control=lambda directory: control(directory, prefix),
                        wait_for_slot=queue_wait(folder), on_wait=waiting, **options)
            except RebuildConfirmationRequired as error:
                omissions.append(f'„{name}” – {_rebuild_omissions(error)}')
                continue
            except ConversionError as error:
                raise ConversionError(f'„{name}”: {error}') from None
            with result, part.open('wb') as target:
                shutil.copyfileobj(result, target)
            write_json(report, {'warnings': list(getattr(result, 'conversion_warnings', []))[:30],
                                'removed_images': getattr(result, 'removed_images', 0),
                                'summary': getattr(result, 'cleaner_summary', None) or {}})
        details = read_json(report)
        warnings.extend(f'„{name}”: {text}' for text in details.get('warnings', []) if isinstance(text, str))
        count = details.get('removed_images', 0)
        removed += count if isinstance(count, int) and not isinstance(count, bool) else 0
        summaries.append(details.get('summary') if isinstance(details.get('summary'), dict) else {})
    if omissions:
        raise RebuildConfirmationRequired(REBUILD_WARNING + '; '.join(omissions[:30]) + MERGE_REBUILD_SUFFIX)
    control(None, 'Scalanie plików')
    merged = merge_documents([(parts / f'{index:02d}.docx').read_bytes() for index in range(1, len(names) + 1)])
    if use_template:
        control(None, 'Nadawanie stylów szablonu')
        merged = apply_template_styles(merged)
    if len(merged) > MAX_OUTPUT:
        raise ConversionError('Scalony dokument przekroczył 50 MB. Scal mniej plików naraz.')
    (folder / 'result').write_bytes(merged)
    order = ', '.join(f'{index}. „{name}”' for index, name in enumerate(names, 1))
    done = {'state': 'done', 'mime': DOCX_MIME,
            'filename': f'Scalone – {len(names)} {plural(len(names), "plik", "pliki", "plików")}.docx',
            'warnings': [f'Kolejność scalania: {order}.', *warnings][:30], 'removed_images': removed}
    if options.get('cleaner_summary'):
        summary = _sum_summaries(summaries)
        done['summary'] = summary_rows(summary)
        done['replacements'] = replacement_rows(summary)
    return done


def _run_split(folder, data, options, *, control, waiting):
    """Podział według Nagłówków 1 w izolowanym podprocesie konwertera (limit pamięci i czasu)."""
    source = folder / 'source.docx'
    with source.open('rb') as stream:
        validate_resources(File(stream, name=data['name']))
    with conversion_slot(queue_wait(folder), waiting) as base, \
            tempfile.TemporaryDirectory(prefix='document-', dir=base) as temporary:
        directory = Path(temporary)
        shutil.copyfile(source, directory / 'source.docx')
        (directory / 'job.json').write_text(json.dumps({'formats': [], 'split': True}), encoding='utf-8')
        control(directory)
        run_converter(directory, job_time_limit(options), control=control)
        try:
            report = json.loads((directory / 'split.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            report = {}
        if report.get('error') == 'no_heading':
            raise ConversionError(NO_HEADING_MESSAGE)
        output = directory / 'document.zip'
        files = report.get('files')
        if (not isinstance(files, list) or not files or not output.is_file() or output.is_symlink()
                or not 0 < output.stat().st_size <= MAX_OUTPUT):
            raise ConversionError('Podział nie utworzył poprawnego archiwum lub wynik przekroczył 50 MB.')
        shutil.copyfile(output, folder / 'result')
    names = [name for name in files if isinstance(name, str)]
    titles = report.get('titles')
    if not isinstance(titles, list) or len(titles) != len(files):
        titles = [''] * len(files)
    # Nagłówki części dla wysyłki autorom (split_mailing): nazwa pliku w ZIP i pełny tekst nagłówka.
    write_json(folder / SPLIT_PARTS, [{'file': name, 'title': title if isinstance(title, str) else ''}
                                      for name, title in zip(files, titles) if isinstance(name, str)])
    warnings = [f'Utworzono {len(names)} {plural(len(names), "plik", "pliki", "plików")}: {", ".join(names)}.']
    if report.get('skipped_intro'):
        warnings.append('Pominięto treść przed pierwszym Nagłówkiem 1 (np. stronę tytułową) – '
                        'nie ma jej w żadnym pliku.')
    # Obrazy połączone z plikami spoza dokumentu – podproces usuwa je razem z linkiem przed podziałem.
    linked = report.get('linked_images', 0)
    if isinstance(linked, int) and not isinstance(linked, bool) and linked > 0:
        warnings.append(linked_images_note(linked))
    return {'state': 'done', 'mime': 'application/zip', 'removed_images': 0, 'warnings': warnings[:30], 'split': True,
            'filename': conversion_filename(Path(data['name']).stem[:120] + ' – podział', 'zip')}


RUNNERS = {'merge': _run_merge, 'split': _run_split}


def execute(folder):
    data = read_json(folder / 'request.json')
    started = time.time()
    last_progress, last_beat = None, 0.0

    def beat(state, extra):
        nonlocal last_beat
        last_beat = time.time()
        write_json(folder / 'state.json', {'state': state, 'started': started, 'heartbeat': last_beat, **extra})

    def waiting():
        if (folder / 'cancel').exists():
            raise JobCancelled()
        if time.time() - last_beat >= 2:
            beat('queued', {'stage': 'W kolejce – konwerter kończy inny dokument'})

    def control(directory, prefix=''):
        """Anulowanie i postęp; prefix opisuje plik scalania („Plik 2 z 5: …”), bez katalogu – sam etap."""
        nonlocal last_progress
        if (folder / 'cancel').exists():
            raise JobCancelled()
        if directory is None:
            progress = {'stage': prefix}
        else:
            try:
                progress = read_json(directory / 'progress.json')
            except (OSError, ValueError):
                progress = last_progress or {'stage': 'Uruchamianie konwertera'}
            if progress.get('stage') == 'start':
                progress = {'stage': 'Uruchamianie konwertera'}
            if prefix:
                progress = {**progress, 'stage': prefix + str(progress.get('stage', ''))}
        if progress != last_progress or time.time() - last_beat >= 5:
            last_progress = progress
            beat('running', progress)

    beat('running', {'stage': 'Sprawdzanie dokumentu'})
    try:
        if (folder / 'cancel').exists():
            raise JobCancelled()
        options = dict(data['options'])
        if (folder / 'accepted').exists():
            options['allow_rebuild_omissions'] = True
        done = RUNNERS.get(data['action'], _run_single)(folder, data, options, control=control, waiting=waiting)
        if (folder / 'cancel').exists():
            raise JobCancelled()
        write_json(folder / 'state.json', done)
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
    for name in ('sources', 'parts'):
        shutil.rmtree(folder / name, ignore_errors=True)
