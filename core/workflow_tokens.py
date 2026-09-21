from django.core import signing
from django.core.exceptions import ValidationError
from core.edit_versions import version_of


def make_token(text, user):
    return signing.dumps({'text': text.pk, 'user': user.pk, 'state': version_of(text)}, salt='workflow-action', compress=True)


def check_token(value, text, user):
    try:
        payload = signing.loads(value, salt='workflow-action', max_age=86400)
        valid = payload['text'] == text.pk and payload['user'] == user.pk and type(payload['state']) is int and payload['state'] == version_of(text)
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValidationError("Stan tekstu lub przydziały zmieniły się albo formularz wygasł. Odśwież podgląd przed wykonaniem operacji.")
