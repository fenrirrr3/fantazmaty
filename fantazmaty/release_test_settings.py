"""Disposable production-shaped rendering tests, never production database data."""
from .test_settings import *
DEBUG = False
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', *MIDDLEWARE,
              'django.middleware.clickjacking.XFrameOptionsMiddleware']
STATIC_ROOT = BASE_DIR / 'var' / 'release-test-static'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'}}
