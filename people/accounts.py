"""Konto logowania podąża za osobą: e-mail osoby jest loginem i nazwą użytkownika, a „wciąż w ekipie” steruje logowaniem.
Wyjątek: konta superuserów nigdy nie są blokowane automatycznie (żeby administrator nie odciął sobie dostępu).

Synchronizację uruchamiają sygnały zapisu osoby (people/identity.py), więc działa przy każdym zapisie, nie tylko w adminie.
Konto zmieniane jest przez QuerySet.update() – bez sygnałów konta, więc bez pętli.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

ACTIVITY_FIELDS = {'is_active', 'is_external'}


def normalize_email(value):
    return (value or '').strip().lower()


def login_allowed(person):
    """Logować się może osoba w ekipie, niebędąca zewnętrzną (brak osoby – nie)."""
    return bool(person and person.is_active and not person.is_external)


def validate_login_email(value, *, exclude_pk=None, using='default'):
    """E-mail jako login i nazwa użytkownika: poprawny i niezajęty przez inne konto (także jako nazwa użytkownika)."""
    from core.account_identity import validate_account_email
    value = validate_account_email(value, exclude_pk=exclude_pk, using=using)
    User = get_user_model()
    if len(value) > User._meta.get_field('username').max_length:
        raise ValidationError('Adres e-mail jest zbyt długi, by służyć jako login.')
    if User.objects.using(using).filter(username__iexact=value).exclude(pk=exclude_pk).exists():
        raise ValidationError('Konto z tym adresem e-mail już istnieje.')
    return value


def usable_login(email):
    """Adres poprawny i mieszczący się w nazwie użytkownika; niepoprawne stare dane sygnał pomija (zgłasza je formularz)."""
    from django.core.validators import validate_email
    try:
        validate_email(email)
    except ValidationError:
        return False
    return len(email) <= get_user_model()._meta.get_field('username').max_length


def account_changes(person, *, using='default', update_fields=None, previous=None):
    """Zmiany konta wynikające z zapisu osoby; ValidationError, gdy e-mail osoby zajmuje inne konto.

    previous – dotychczasowe {user_id, is_active, is_external} osoby (None dla nowej).
    Logowanie przestawiamy tylko przy zmianie „wciąż w ekipie”/„zewnętrzny”, żeby zwykła edycja osoby nie odblokowywała
    konta zablokowanego wcześniej z innego powodu. Powiązanie konta z osobą spoza ekipy tylko je blokuje.
    """
    if not person.user_id:
        return {}
    fields = set(update_fields) if update_fields is not None else None
    account = (get_user_model().objects.using(using).filter(pk=person.user_id)
               .values('email', 'username', 'is_superuser').first())
    if account is None:
        return {}
    linked = previous is None or previous['user_id'] != person.user_id
    changes = {}
    email = normalize_email(person.email)
    if email and (fields is None or linked or 'email' in fields) and (account['email'], account['username']) != (email, email):
        if usable_login(email):
            # Kolizja z innym kontem przerywa zapis osoby; formularze zgłaszają ją wcześniej jako błąd pola.
            changes.update(email=validate_login_email(email, exclude_pk=person.user_id, using=using))
            changes['username'] = changes['email']
    activity_changed = previous is not None and (previous['is_active'], previous['is_external']) != (person.is_active, person.is_external)
    if (fields is None or fields & ACTIVITY_FIELDS) and activity_changed and not linked:
        changes['is_active'] = login_allowed(person)
    elif linked and not login_allowed(person):
        changes['is_active'] = False
    if account['is_superuser'] and changes.get('is_active') is False:
        del changes['is_active']
    return changes


def apply_account_changes(user_id, changes, using='default'):
    if not changes:
        return
    from core.edit_versions import bump
    User = get_user_model()
    User.objects.using(using).filter(pk=user_id).update(**changes)
    bump(User._meta.label_lower, user_id, using)
