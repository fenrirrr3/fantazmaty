"""Record startup failures without logging source lines or document content."""
import json
import runpy
import sys
import traceback
from pathlib import Path

directory = Path(sys.argv[1])
try:
    (directory / 'progress.json').write_text(json.dumps({'stage': 'start', 'python': sys.version.split()[0]}))
    runpy.run_path(str(Path(__file__).with_name('document_conversion_worker.py')), run_name='__main__')
except SystemExit:
    raise
except BaseException as error:
    report = {'stage': 'start', 'error': type(error).__name__, 'python': sys.version.split()[0],
              'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                         for frame in traceback.extract_tb(error.__traceback__)]}
    try:
        (directory / 'error.json').write_text(json.dumps(report), encoding='utf-8')
    finally:
        sys.exit(2 if isinstance(error, ImportError) else 3)
