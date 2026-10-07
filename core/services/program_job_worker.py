"""Trusted supervisor. The document itself still runs in the bounded converter."""
import os
import sys
from pathlib import Path

if __name__ == '__main__':
    if os.name == 'posix':
        os.umask(0o077)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fantazmaty.settings')
    folder = Path(sys.argv[1])
    try:
        import django
        django.setup()
        from django.conf import settings
        settings.DOCUMENT_CONVERSION_DIR = folder.parent.parent
        from core.services.program_jobs import execute
        execute(folder)
    except Exception as error:
        import json
        print('Program startup error: ' + type(error).__name__, file=sys.stderr)
        temporary = folder / 'state.tmp'
        temporary.write_text(json.dumps({'state': 'error', 'message': 'Nie można uruchomić programu. Administrator musi sprawdzić środowisko Pythona i log zadania.'}), encoding='utf-8')
        os.replace(temporary, folder / 'state.json')
        sys.exit(1)
