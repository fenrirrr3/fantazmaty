"""Disposable production-shaped rendering tests, never production database data."""
from .test_settings import *  # noqa: F403

DEBUG = False
STATIC_ROOT = BASE_DIR / "var" / "release-test-static"  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
