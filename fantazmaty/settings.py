import os
from pathlib import Path
from dotenv import load_dotenv

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(
    BASE_DIR / ".env",
    override=False,
    interpolate=False,
    encoding="utf-8",
)

def env_bool(name, default=False):
    value = os.environ.get(name)

    if value is None:
        return default

    normalized = value.strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True

    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ImproperlyConfigured(
        f"{name} musi mieć wartość true/false albo 1/0."
    )


def env_int(name, default, minimum=0):
    raw_value = os.environ.get(name, str(default))

    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            f"{name} musi być liczbą całkowitą."
        ) from exc

    if value < minimum:
        raise ImproperlyConfigured(
            f"{name} nie może być mniejsze niż {minimum}."
        )

    return value


def env_list(name, default=""):
    return [
        item.strip()
        for item in os.environ.get(name, default).split(",")
        if item.strip()
    ]


def env_required(name):
    value = os.environ.get(name)

    if value is None or not value.strip():
        raise ImproperlyConfigured(
            f"Brak wymaganej zmiennej środowiskowej: {name}."
        )

    return value


# Zmienne muszą zostać przekazane przez środowisko procesu.
# Ten moduł nie wczytuje automatycznie pliku .env.
DJANGO_ENV = os.environ.get(
    "DJANGO_ENV",
    "development",
).strip().lower()

if DJANGO_ENV not in {"development", "test", "production"}:
    raise ImproperlyConfigured(
        "DJANGO_ENV musi mieć wartość development, test albo production."
    )

IS_PRODUCTION = DJANGO_ENV == "production"

SECRET_KEY = env_required("DJANGO_SECRET_KEY")

DEBUG = env_bool(
    "DJANGO_DEBUG",
    default=DJANGO_ENV == "development",
)

if IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured(
        "DJANGO_DEBUG musi być wyłączone w środowisku produkcyjnym."
    )

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    default="" if IS_PRODUCTION else "localhost,127.0.0.1,[::1]",
)

if IS_PRODUCTION and (not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS):
    raise ImproperlyConfigured(
        "Na produkcji ustaw DJANGO_ALLOWED_HOSTS na konkretne nazwy hostów."
    )

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "authors.apps.AuthorsConfig",
    "texts.apps.TextsConfig",
    "workflow.apps.WorkflowConfig",
    "people.apps.PeopleConfig",
    "core.apps.CoreConfig",
    "illustrations.apps.IllustrationsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.EditingMiddleware",
]

ROOT_URLCONF = "fantazmaty.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Pierwszeństwo własnych szablonów, również admin/base_site.html.
        "DIRS": [
            BASE_DIR / "core" / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "fantazmaty.wsgi.application"
ASGI_APPLICATION = "fantazmaty.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Projekt korzysta z MySQL w środowisku lokalnym, testowym i produkcyjnym.
DB_ENGINE = os.environ.get("DJANGO_DB_ENGINE", "mysql").strip().lower()
if DB_ENGINE not in {"mysql", "django.db.backends.mysql"}:
    raise ImproperlyConfigured(
        "Projekt wymaga MySQL. Ustaw DJANGO_DB_ENGINE=mysql."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env_required("DJANGO_DB_NAME"),
        "USER": env_required("DJANGO_DB_USER"),
        "PASSWORD": env_required("DJANGO_DB_PASSWORD"),
        "HOST": os.environ.get("DJANGO_DB_HOST", "127.0.0.1"),
        "PORT": env_int("DJANGO_DB_PORT", 3306, minimum=1),
        "CONN_MAX_AGE": env_int("DJANGO_DB_CONN_MAX_AGE", 0),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "charset": "utf8mb4",
            "isolation_level": "read committed",
            "sql_mode": "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
            "init_command": "SET default_storage_engine=INNODB",
            "connect_timeout": env_int("DJANGO_DB_CONNECT_TIMEOUT", 10, minimum=1),
        },
        "TEST": {
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_0900_as_ci",
        },
    },
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
        "OPTIONS": {
            "min_length": 12,
        },
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "login"

PASSWORD_RESET_TIMEOUT = env_int(
    "DJANGO_PASSWORD_RESET_TIMEOUT",
    3600,
    minimum=1,
)

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "fantazmaty_sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool(
    "DJANGO_SESSION_COOKIE_SECURE",
    default=IS_PRODUCTION,
)
SESSION_COOKIE_AGE = env_int(
    "DJANGO_SESSION_COOKIE_AGE",
    8 * 60 * 60,
    minimum=1,
)
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool(
    "DJANGO_SESSION_EXPIRE_AT_BROWSER_CLOSE",
    default=True,
)

CSRF_COOKIE_NAME = "fantazmaty_csrftoken"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = env_bool(
    "DJANGO_CSRF_COOKIE_SECURE",
    default=IS_PRODUCTION,
)
# Umożliwia istniejącym skryptom odczyt tokena dla żądań AJAX.
CSRF_COOKIE_HTTPONLY = False


SECURE_SSL_REDIRECT = env_bool(
    "DJANGO_SECURE_SSL_REDIRECT",
    default=IS_PRODUCTION,
)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Włącz dopiero po sprawdzeniu poprawnej obsługi HTTPS.
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=False,
)
SECURE_HSTS_PRELOAD = env_bool(
    "DJANGO_SECURE_HSTS_PRELOAD",
    default=False,
)

# Włącz tylko za zaufanym proxy, które usuwa nagłówek klienta
# i samodzielnie ustawia X-Forwarded-Proto.
if env_bool("DJANGO_TRUST_PROXY_SSL_HEADER", default=False):
    SECURE_PROXY_SSL_HEADER = (
        "HTTP_X_FORWARDED_PROTO",
        "https",
    )

USE_X_FORWARDED_HOST = False

if IS_PRODUCTION and not all(
    [
        SESSION_COOKIE_SECURE,
        CSRF_COOKIE_SECURE,
        SECURE_SSL_REDIRECT,
    ]
):
    raise ImproperlyConfigured(
        "Na produkcji wymagane są bezpieczne cookies sesji i CSRF "
        "oraz przekierowanie na HTTPS."
    )


LANGUAGE_CODE = "pl"
TIME_ZONE = "Europe/Warsaw"
USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Na produkcji STATIC_ROOT musi być obsługiwany przez serwer WWW.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
            if IS_PRODUCTION
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
}


EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND",
    (
        "django.core.mail.backends.smtp.EmailBackend"
        if IS_PRODUCTION
        else "django.core.mail.backends.locmem.EmailBackend"
    ),
)

DEFAULT_FROM_EMAIL = os.environ.get(
    "DJANGO_DEFAULT_FROM_EMAIL",
    "Fantazmaty CMS <noreply@fantazmaty.pl>",
)
SERVER_EMAIL = os.environ.get(
    "DJANGO_SERVER_EMAIL",
    DEFAULT_FROM_EMAIL,
)

EMAIL_HOST = os.environ.get("DJANGO_EMAIL_HOST", "")
EMAIL_PORT = env_int("DJANGO_EMAIL_PORT", 587, minimum=1)
EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_HOST_PASSWORD", "")

EMAIL_USE_SSL = env_bool("DJANGO_EMAIL_USE_SSL", default=False)
EMAIL_USE_TLS = env_bool(
    "DJANGO_EMAIL_USE_TLS",
    default=not EMAIL_USE_SSL,
)
EMAIL_TIMEOUT = env_int("DJANGO_EMAIL_TIMEOUT", 15, minimum=1)

if EMAIL_USE_SSL and EMAIL_USE_TLS:
    raise ImproperlyConfigured(
        "DJANGO_EMAIL_USE_SSL i DJANGO_EMAIL_USE_TLS "
        "nie mogą być włączone jednocześnie."
    )

if (
    IS_PRODUCTION
    and EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend"
    and not EMAIL_HOST.strip()
):
    raise ImproperlyConfigured(
        "Ustaw DJANGO_EMAIL_HOST dla produkcyjnego backendu SMTP."
    )


LOG_LEVEL = os.environ.get("DJANGO_LOG_LEVEL", "INFO").strip().upper()

if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    raise ImproperlyConfigured(
        "Nieprawidłowe DJANGO_LOG_LEVEL."
    )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
        "null": {
            "class": "logging.NullHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        # Zapytania SQL mogą zawierać dane osobowe.
        "django.db.backends": {
            "handlers": ["null"],
            "propagate": False,
        },
    },
}
