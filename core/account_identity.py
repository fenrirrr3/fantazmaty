"""Email login rules shared by the admin and createsuperuser command."""
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
