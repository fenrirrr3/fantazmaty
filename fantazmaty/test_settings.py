"""Ustawienia testów: konfiguracja produkcyjna z izolowaną bazą SQLite.

Moduł importuje fantazmaty.settings, więc kolejność middleware, język,
strefa czasowa, przekierowania i nagłówki bezpieczeństwa są takie same
jak w działającej aplikacji. Nadpisywane są tylko baza, hasher haseł
i ścieżki plików roboczych. Lokalny .env jest pomijany.
"""
import os
import tempfile
from pathlib import Path

for _name, _value in {
    "DJANGO_SKIP_DOTENV": "1",
    "DJANGO_ENV": "test",
    "DJANGO_SECRET_KEY": "isolated-tests-only-not-a-real-secret-0123456789",
    "DJANGO_DB_NAME": "unused",
    "DJANGO_DB_USER": "unused",
    "DJANGO_DB_PASSWORD": "unused",
}.items():
    os.environ.setdefault(_name, _value)

from .settings import *  # noqa: E402,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Pliki robocze testów nie trafiają do var/ w katalogu projektu.
_WORK_DIR = Path(tempfile.gettempdir()) / "fantazmaty-tests"
ACTIVITY_SPOOL_DIR = _WORK_DIR / "activity-spool"
DOCUMENT_CONVERSION_DIR = _WORK_DIR / "conversion"
