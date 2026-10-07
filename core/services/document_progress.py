"""Progress of the isolated converter; contains no document contents."""
import json
import os
from pathlib import Path

_target = None


def configure(directory):
    global _target
    _target = Path(directory) / 'progress.json'


def report(stage, completed=None, total=None):
    if _target is None:
        return
    data = {'stage': stage}
    if total and completed is not None:
        data['completed'] = completed
        data['total'] = total
    temporary = _target.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(data), encoding='utf-8')
        os.replace(temporary, _target)
    except OSError:
        # A missing/unavailable progress file must not invalidate the document.
        pass
