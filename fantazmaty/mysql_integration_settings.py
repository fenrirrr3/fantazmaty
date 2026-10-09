"""Isolated integration tests: never point TEST_MYSQL_DATABASE at a live database.

Opcje połączenia (izolacja READ COMMITTED, sql_mode, kolacja testowej bazy)
są kopiowane z ustawień produkcyjnych, aby blokady i porównania tekstu
działały tak samo jak na serwerze.
"""
import os

from .settings import DATABASES as _PRODUCTION_DATABASES
from .test_settings import *  # noqa: F403

_production = _PRODUCTION_DATABASES["default"]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.mysql",
    "NAME": os.environ.get("TEST_MYSQL_DATABASE", "fantazmaty_integration"),
    "USER": os.environ["TEST_MYSQL_USER"],
    "PASSWORD": os.environ["TEST_MYSQL_PASSWORD"],
    "HOST": os.environ.get("TEST_MYSQL_HOST", "127.0.0.1"),
    "PORT": os.environ.get("TEST_MYSQL_PORT", "3306"),
    "CONN_HEALTH_CHECKS": True,
    "OPTIONS": {**_production["OPTIONS"]},
    "TEST": {
        **_production["TEST"],
        "NAME": os.environ.get("TEST_MYSQL_TEST_DATABASE", "test_fantazmaty_integration"),
    },
}}
if (
    not DATABASES["default"]["TEST"]["NAME"].startswith("test_")
    or DATABASES["default"]["NAME"] == DATABASES["default"]["TEST"]["NAME"]
):
    raise RuntimeError("Testy wymagają osobnej bazy o nazwie zaczynającej się od test_.")
