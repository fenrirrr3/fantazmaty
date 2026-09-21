SECRET_KEY='isolated-tests-only'
INSTALLED_APPS=['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','authors.apps.AuthorsConfig','texts.apps.TextsConfig','workflow.apps.WorkflowConfig','people.apps.PeopleConfig','core.apps.CoreConfig','illustrations.apps.IllustrationsConfig']
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}
MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','django.contrib.messages.middleware.MessageMiddleware']
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','APP_DIRS':True,'OPTIONS':{'builtins':['core.templatetags.editing'],'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages']}}]
ROOT_URLCONF='fantazmaty.urls'
STATIC_URL='/static/'
USE_TZ=True
TIME_ZONE='Europe/Warsaw'
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
ALLOWED_HOSTS=['testserver','localhost','127.0.0.1']
LOGIN_URL='/accounts/login/'
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parent.parent
MIDDLEWARE += ['core.activity.UserActivityMiddleware', 'core.middleware.EditingMiddleware']

TEMPLATES[0]["DIRS"] = [BASE_DIR / "core" / "templates"]
