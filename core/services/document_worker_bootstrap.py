"""Record startup failures without logging source lines or document content."""
import gc
import json
import runpy
import sys
import traceback
from pathlib import Path

# Limit przestrzeni adresowej procesu roboczego (MB). Mały DOCX może rozpakować się do dziesiątek MB XML,
# a python-docx, Mammoth i lxml potrzebują wielokrotności tego w pamięci; po przekroczeniu limitu proces
# kończy się błędem MemoryError zamiast zajmować pamięć całego serwera. Analiza powtórzeń ze spaCy (model pl)
# mieści się z zapasem. job.json może podać własny limit ("memory_limit_mb"; 0 = bez limitu).
DEFAULT_MEMORY_LIMIT_MB = 1536
MINIMUM_MEMORY_LIMIT_MB = 512


def memory_limit_mb(directory):
    try:
        value = json.loads((directory / 'job.json').read_text(encoding='utf-8')).get('memory_limit_mb')
    except (OSError, ValueError, AttributeError):
        value = None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return DEFAULT_MEMORY_LIMIT_MB
    return value and max(value, MINIMUM_MEMORY_LIMIT_MB)


def limit_memory(directory):
    """RLIMIT_AS dla tego procesu (tylko systemy z modułem resource, czyli nie Windows)."""
    try:
        import resource
    except ImportError:
        return
    megabytes = memory_limit_mb(directory)
    if not megabytes:
        return
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = megabytes * 1024 * 1024
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def release_frames(error):
    """Po MemoryError zwolnij zmienne lokalne z ramek wyjątku (i wyjątków, w trakcie których powstał).

    Inaczej ślad wyjątku trzyma w pamięci cały wczytany dokument i nie da się zapisać nawet raportu błędu.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        traceback.clear_frames(error.__traceback__)
        error = error.__context__
    gc.collect()


directory = Path(sys.argv[1])
stage = 'start'
# Rezerwa zwalniana po MemoryError, żeby starczyło pamięci na raport błędu.
reserve = bytearray(8 * 1024 * 1024)
try:
    (directory / 'progress.json').write_text(json.dumps({'stage': 'start', 'python': sys.version.split()[0]}))
    limit_memory(directory)
    stage = 'konwersja'
    runpy.run_path(str(Path(__file__).with_name('document_conversion_worker.py')), run_name='__main__')
except SystemExit:
    raise
except BaseException as error:
    reserve = None
    # Gdy pamięci zabrakło w trakcie obsługi innego błędu, ważniejsze jest miejsce pierwszego braku.
    first = error.__context__ if isinstance(error.__context__, MemoryError) else error
    frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
              for frame in traceback.extract_tb(first.__traceback__)]
    first = None
    if isinstance(error, MemoryError) or isinstance(error.__context__, MemoryError):
        release_frames(error)
        error = MemoryError()  # brak pamięci także wtedy, gdy zabrakło jej dopiero przy raporcie błędu
    report = {'stage': stage, 'error': type(error).__name__, 'python': sys.version.split()[0], 'frames': frames}
    try:
        (directory / 'error.json').write_text(json.dumps(report), encoding='utf-8')
    finally:
        sys.exit(2 if isinstance(error, ImportError) else 3)
