"""Isolated integration tests: never point TEST_MYSQL_DATABASE at a live database."""
import os
from .test_settings import *
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.mysql',
    'NAME': os.environ.get('TEST_MYSQL_DATABASE','fantazmaty_integration'),
    'USER': os.environ['TEST_MYSQL_USER'],
    'PASSWORD': os.environ['TEST_MYSQL_PASSWORD'],
    'HOST': os.environ.get('TEST_MYSQL_HOST','127.0.0.1'),
    'PORT': os.environ.get('TEST_MYSQL_PORT','3306'),
    'OPTIONS': {'charset':'utf8mb4', 'init_command':"SET sql_mode='STRICT_TRANS_TABLES'"},
    'TEST': {'NAME': os.environ.get('TEST_MYSQL_TEST_DATABASE','test_fantazmaty_integration')},
}}
if not DATABASES['default']['TEST']['NAME'].startswith('test_') or DATABASES['default']['NAME'] == DATABASES['default']['TEST']['NAME']:
    raise RuntimeError('Testy wymagają osobnej bazy o nazwie zaczynającej się od test_.')
