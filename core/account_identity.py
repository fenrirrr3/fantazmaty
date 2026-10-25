"""Konto: reguły e-maila logowania (admin, createsuperuser) i podpis osoby z konta."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


def validate_account_email(value, *, exclude_pk=None, using="default"):
    value = (value or "").strip().lower()
    if not value:
        raise ValidationError("Podaj adres e-mail służący do logowania.")
    validate_email(value)
    if len(value) > get_user_model()._meta.get_field("email").max_length:
        raise ValidationError("Adres e-mail jest zbyt długi.")
    if get_user_model().objects.using(using).filter(email__iexact=value).exclude(pk=exclude_pk).exists():
        raise ValidationError("Konto z tym adresem e-mail już istnieje.")
    return value


def person_name(user):
    """Imię i nazwisko z profilu osoby; bez profilu – z konta (imię i nazwisko, a gdy puste – login)."""
    if user is None:
        return ''
    person = getattr(user, 'person_profile', None)
    return (str(person) if person else '') or user.get_full_name() or user.get_username()
