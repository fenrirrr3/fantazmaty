"""Bounded library-based conversion in a disposable Python subprocess."""
import os
import time
import sys
import json
import logging
import re
import posixpath
import shutil
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, SpooledTemporaryFile
from zipfile import ZipFile, ZIP_DEFLATED

from django.conf import settings
from lxml import etree

logger = logging.getLogger(__name__)

FORMATS = {'pdf': 'application/pdf', 'epub': 'application/epub+zip'}
MAX_OUTPUT = 50 * 1024 * 1024
TIME_LIMIT = 90


REBUILD_WARNING = 'W nowym DOCX zostaną pominięte: '


class ConversionError(Exception):
    pass



class RebuildConfirmationRequired(ConversionError):
    pass


@contextmanager
def conversion_slot():
    """One conversion per shared project directory, across web workers."""
    directory = Path(getattr(settings, 'DOCUMENT_CONVERSION_DIR', settings.BASE_DIR / 'var' / 'conversion'))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'conversion.lock').open('a+b') as lock:
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConversionError('Konwerter przetwarza teraz inny dokument. Spróbuj za chwilę.') from None
        else:
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ConversionError('Konwerter przetwarza teraz inny dokument. Spróbuj za chwilę.') from None
        try:
            yield directory
        finally:
            if os.name == 'posix':
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def validate_resources(source):
    """Do not let conversion fetch linked files, remote images or templates."""
    source.seek(0)
    try:
        with ZipFile(source) as archive:
            names = archive.namelist()
            if any(name.startswith(('/', '\\')) or '\\' in name or '..' in name.split('/') for name in names):
                raise ConversionError('Nieprawidłowa struktura dokumentu DOCX.')
            if len(names) != len(set(names)):
                raise ConversionError('Dokument zawiera powielone elementy. Zapisz go ponownie w Wordzie.')
            for name in names:
                if name.lower().endswith('.rels'):
                    root = etree.fromstring(archive.read(name), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
                    if root.getroottree().docinfo.doctype:
                        raise ConversionError('Nieobsługiwany dokument DOCX.')
                    for relationship in root:
                        target = relationship.get('Target', '')
                        if relationship.get('TargetMode', '').lower() != 'external':
                            base = posixpath.dirname(posixpath.dirname(name))
                            resolved = posixpath.normpath(posixpath.join(base, target))
                            if target.startswith(('/', '\\')) or ':' in target or '\\' in target or resolved == '..' or resolved.startswith('../'):
                                raise ConversionError('Dokument zawiera nieprawidłowe odwołanie do pliku.')
                        if relationship.get('TargetMode', '').lower() == 'external':
                            if not relationship.get('Type', '').endswith('/hyperlink'):
                                raise ConversionError('Dokument zawiera zewnętrzny obraz lub inny podłączony plik. Osadź go w DOCX przed konwersją.')
    finally:
        source.seek(0)


def converter_python():
    """WSGI's sys.executable may be uwsgi, not a Python interpreter."""
    configured = getattr(settings, 'DOCUMENT_CONVERTER_PYTHON', '') or os.environ.get('DOCUMENT_CONVERTER_PYTHON', '')
    def usable(path):
        return path.is_file() and (os.name == 'nt' or os.access(path, os.X_OK))
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute() or not usable(candidate):
            raise ConversionError('DOCUMENT_CONVERTER_PYTHON musi wskazywać istniejący interpreter Pythona (pełna ścieżka).')
        return str(candidate)
    import django
    roots = [parent for parent in Path(django.__file__).absolute().parents
             if (parent / 'pyvenv.cfg').is_file()]
    roots.append(Path(sys.prefix))
    for root in roots:
        candidate = root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if usable(candidate):
            # Do not resolve symlinks: the venv path selects its site-packages.
            return str(candidate.absolute())
    candidate = Path(sys.executable)
    if re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?', candidate.name, re.IGNORECASE) and usable(candidate):
        return str(candidate.absolute())
    raise ConversionError('Nie znaleziono Pythona do konwersji. Ustaw DOCUMENT_CONVERTER_PYTHON na interpreter środowiska projektu.')


def run_converter(directory, timeout):
    if timeout <= 0:
        raise ConversionError('Konwersja przekroczyła limit 90 sekund. Podziel dokument lub wybierz mniej formatów.')
    # Do not pass project credentials to an external document processor.
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'PYTHONPATH')}
    env.update(HOME=str(directory), TMPDIR=str(directory), TEMP=str(directory), TMP=str(directory),
               PYTHONDONTWRITEBYTECODE='1')
    command = [converter_python(), str(Path(__file__).with_name('document_worker_bootstrap.py')), str(directory)]
    try:
        with subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=os.name == 'posix') as process:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == 'posix':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                raise ConversionError('Konwersja przekroczyła limit 90 sekund. Podziel dokument lub wybierz mniej formatów.') from None
    except OSError:
        raise ConversionError('Nie można uruchomić konwertera. Administrator musi sprawdzić środowisko Pythona na serwerze.') from None
    if code != 0:
        report = {}
        try:
            error_file = directory / 'error.json'
            if error_file.stat().st_size <= 32768:
                report = json.loads(error_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if not report:
            report = {'stage': 'start', 'error': 'ProcessTerminated', 'exit': code,
                      'signal': -code if code < 0 else None}
        if report.get('error') != 'RebuildUnsupported':
            logger.error('Błąd konwertera: exit=%s python=%s diagnostics=%s', code, command[0], report)
        stage = report.get('stage', '')
        stage = stage if stage in ('start', 'DOCX', 'PDF', 'EPUB', 'Powtórzenia') else 'konwersja'
        kind = report.get('error', '')
        kind = kind if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,80}', kind) else 'błąd procesu'
        if kind == 'RebuildUnsupported':
            raise RebuildConfirmationRequired(REBUILD_WARNING + '; '.join(str(item) for item in report.get('omissions', [])[:30]) + '. Po akceptacji powstanie nowy DOCX bez tych elementów. Tekst i obsługiwane formatowanie zostaną przeniesione.')
        from .document_errors import MESSAGES
        public_code = report.get('public_code')
        if kind == 'DocumentInputError' and isinstance(public_code, str) and public_code in MESSAGES:
            raise ConversionError(MESSAGES[public_code])
        detail = f'{stage}: {kind}'
        if code == 2:
            raise ConversionError('Brakuje bibliotek konwertera. Zainstaluj requirements.txt. ' + detail + '. Szczegóły zapisano w logu błędów.')
        raise ConversionError('Nie udało się przygotować plików (' + detail + '). Szczegóły zapisano w logu błędów. Nie oznacza to automatycznie uszkodzenia dokumentu; możesz pobrać oryginały po wyłączeniu konwersji.')


def conversion_filename(name, extension):
    # Preserve Unicode and spaces, but never put paths or controls in a ZIP entry.
    basename = str(name or 'Dokument.docx').replace('\\', '/').rsplit('/', 1)[-1]
    stem = re.sub(r'[\x00-\x1f\x7f]', '', Path(basename).stem).strip() or 'Dokument'
    if stem in ('.', '..'):
        stem = 'Dokument'
    return stem + '.' + extension


def convert_document(upload, formats, *, use_cleaner=False, timeout=TIME_LIMIT, include_docx=False, rebuild=False, normalize=True, allow_rebuild_omissions=False, cleaner_rules=None, repetitions=None, justify=False, preserve_filename=False, remove_soft_whitespace=False):
    deadline = time.monotonic() + min(TIME_LIMIT, timeout)
    selected = [kind for kind in FORMATS if kind in formats]
    if (not selected and not include_docx) or set(formats) - set(FORMATS):
        raise ConversionError('Wybierz co najmniej jeden obsługiwany format.')
    validate_resources(upload)
    result = SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode='w+b')
    try:
        with conversion_slot() as root, TemporaryDirectory(prefix='document-', dir=root) as temporary:
            directory = Path(temporary)
            source = _write_job(directory, upload, {
                'formats': selected, 'prepare': True, 'clean': use_cleaner,
                'cleaner_rules': cleaner_rules, 'rebuild': rebuild,
                'allow_rebuild_omissions': allow_rebuild_omissions,
                'remove_soft_whitespace': remove_soft_whitespace, 'normalize': normalize, 'justify': justify, 'include_docx': include_docx, 'repetitions': repetitions,
            })
            run_converter(directory, deadline - time.monotonic())
            warning_file = directory / 'warnings.json'
            warnings = json.loads(warning_file.read_text(encoding='utf-8')) if warning_file.is_file() else []
            result.conversion_warnings = warnings
            outputs = [directory / ('document.' + kind) for kind in selected]
            if include_docx:
                shutil.copyfile(source, directory / 'document.docx')
                outputs.append(directory / 'document.docx')
            for target in outputs:
                if not target.is_file() or target.is_symlink() or not 0 < target.stat().st_size <= MAX_OUTPUT:
                    raise ConversionError('Konwersja nie utworzyła poprawnego pliku wynikowego lub wynik przekroczył 50 MB.')
            if len(outputs) == 1 and not warnings:
                with outputs[0].open('rb') as converted:
                    shutil.copyfileobj(converted, result)
                extension = outputs[0].suffix.lstrip('.')
                mime = FORMATS.get(extension, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            else:
                with ZipFile(result, 'w', compression=ZIP_DEFLATED) as archive:
                    for output in outputs:
                        archive.write(output, arcname=conversion_filename(getattr(upload, 'name', 'Dokument.docx'), output.suffix.lstrip('.')) if preserve_filename else output.name)
                    if warnings:
                        archive.writestr('Uwagi_konwersji.txt', '\n'.join(warnings))
                extension, mime = 'zip', 'application/zip'
        if time.monotonic() > deadline:
            raise ConversionError('Przygotowanie dokumentu przekroczyło limit czasu. Wybierz mniej formatów lub krótszy dokument.')
        result.seek(0)
        return result, extension, mime
    except BaseException:
        result.close()
        raise


def _write_job(directory, upload, config):
    source = directory / 'source.docx'
    upload.seek(0)
    with source.open('wb') as destination:
        shutil.copyfileobj(upload, destination)
    config['title'] = Path(getattr(upload, 'name', 'Dokument.docx')).stem[:200] or 'Dokument'
    (directory / 'job.json').write_text(json.dumps(config), encoding='utf-8')
    return source


def inspect_document(upload, *, timeout=TIME_LIMIT):
    """Bounded rebuild preflight; no converted document is constructed."""
    deadline = time.monotonic() + min(TIME_LIMIT, timeout)
    validate_resources(upload)
    with conversion_slot() as root, TemporaryDirectory(prefix='document-', dir=root) as temporary:
        directory = Path(temporary)
        _write_job(directory, upload, {'formats': [], 'inspect': True})
        run_converter(directory, deadline - time.monotonic())
