"""Bounded library-based conversion in a disposable Python subprocess."""
import os
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
from core.services.odkurzacz import clean_docx, ALL_EDITORIAL_RULES

logger = logging.getLogger(__name__)

FORMATS = {'pdf': 'application/pdf', 'epub': 'application/epub+zip'}
MAX_OUTPUT = 50 * 1024 * 1024
TIME_LIMIT = 90


class ConversionError(Exception):
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


def run_converter(directory, timeout):
    if timeout <= 0:
        raise ConversionError('Konwersja przekroczyła limit 90 sekund. Podziel dokument lub wybierz mniej formatów.')
    # Do not pass project credentials to an external document processor.
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'PYTHONPATH')}
    env.update(HOME=str(directory), TMPDIR=str(directory), TEMP=str(directory), TMP=str(directory),
               PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, str(Path(__file__).with_name('document_conversion_worker.py')), str(directory)]
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
        logger.error('Błąd konwertera: exit=%s diagnostics=%s', code, report)
        stage = report.get('stage', '')
        stage = stage if stage in ('DOCX', 'PDF', 'EPUB') else 'konwersja'
        kind = report.get('error', '')
        kind = kind if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,80}', kind) else 'błąd procesu'
        detail = f'{stage}: {kind}'
        if code == 2:
            raise ConversionError('Brakuje bibliotek konwertera. Zainstaluj requirements.txt. ' + detail + '. Szczegóły zapisano w logu błędów.')
        raise ConversionError('Nie udało się przygotować plików (' + detail + '). Szczegóły zapisano w logu błędów. Nie oznacza to automatycznie uszkodzenia dokumentu; możesz pobrać oryginały po wyłączeniu konwersji.')


def convert_document(upload, formats, *, use_cleaner=False, timeout=TIME_LIMIT):
    selected = [kind for kind in FORMATS if kind in formats]
    if not selected or set(formats) - set(FORMATS):
        raise ConversionError('Wybierz co najmniej jeden obsługiwany format.')
    validate_resources(upload)
    result = SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode='w+b')
    try:
        with conversion_slot() as root, TemporaryDirectory(prefix='document-', dir=root) as temporary:
            directory = Path(temporary)
            source = directory / 'source.docx'
            if use_cleaner:
                with clean_docx(upload, ALL_EDITORIAL_RULES) as cleaned, source.open('wb') as destination:
                    shutil.copyfileobj(cleaned, destination)
            else:
                upload.seek(0)
                with source.open('wb') as destination:
                    shutil.copyfileobj(upload, destination)
            title = Path(getattr(upload, 'name', 'Dokument.docx')).stem[:200] or 'Dokument'
            (directory / 'job.json').write_text(json.dumps({'formats': selected, 'title': title}), encoding='utf-8')
            run_converter(directory, min(TIME_LIMIT, timeout))
            outputs = [directory / ('document.' + kind) for kind in selected]
            for target in outputs:
                if not target.is_file() or target.is_symlink() or not 0 < target.stat().st_size <= MAX_OUTPUT:
                    raise ConversionError('Konwersja nie utworzyła poprawnego pliku wynikowego lub wynik przekroczył 50 MB.')
            if len(outputs) == 1:
                with outputs[0].open('rb') as converted:
                    shutil.copyfileobj(converted, result)
                extension = selected[0]
                mime = FORMATS[extension]
            else:
                with ZipFile(result, 'w', compression=ZIP_DEFLATED) as archive:
                    for output in outputs:
                        archive.write(output, arcname=output.name)
                extension, mime = 'zip', 'application/zip'
        result.seek(0)
        return result, extension, mime
    except BaseException:
        result.close()
        raise
