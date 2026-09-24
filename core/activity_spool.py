"""Durable, private spool for request metadata (never request bodies)."""
import json
import logging
import os
from pathlib import Path
import tempfile
import uuid
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

def spool_directory():
    return Path(getattr(settings, 'ACTIVITY_SPOOL_DIR', Path(settings.BASE_DIR) / 'var' / 'activity-spool'))

def enqueue_activity(**data):
    directory = spool_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = uuid.uuid4().hex
    data.update(source_key=key, created_at=timezone.now().isoformat())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, prefix='.', delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / (key + '.json'))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
