"""Wspólne reguły dostępu dla widoków i serwisów.

Role dotyczą wyłącznie aktywnych członków zespołu. Samo is_staff
ani uprawnienie Django do modelu nie daje dostępu do danych autora.

Moduł nie importuje widoków, formularzy ani serwisów workflow,
aby nie tworzyć cyklicznych zależności.
"""

from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import PermissionDenied
from people import roles
from people.models import Person


_read_access = ContextVar("read_access", default=None)


@contextmanager
def read_access_scope():
    """Only read-only views use this cache; mutations always read fresh permissions."""
    if _read_access.get() is not None:
        yield
        return
    token = _read_access.set({})
    try:
        yield
    finally:
        _read_access.reset(token)


class ReadAccessScopeMiddleware:
    """Share permission lookups across one GET/HEAD request.

    Templates, context processors and views ask for the same profile and
    roles many times per page. Read-only requests reuse one lookup; every
    other method keeps reading fresh data, as mutations require.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in {"GET", "HEAD"}:
            with read_access_scope():
                return self.get_response(request)
        return self.get_response(request)


def _role_names(person):
    cache = _read_access.get()
    key = ("roles", person.pk)
    if cache is not None and key in cache:
        return cache[key]
    names = {name.casefold() for name in person.roles.values_list("name", flat=True)}
    if cache is not None:
        cache[key] = names
    return names


def is_active_user(user):
    return bool(
        user is not None
        and user.is_authenticated
        and user.is_active
        and user.pk is not None
    )


def is_superuser(user):
    return is_active_user(user) and bool(user.is_superuser)


def get_active_person_profile(user):
    """Zwraca aktualny aktywny profil, bez korzystania z cache relacji."""
    if not is_active_user(user):
        return None

    cache = _read_access.get()
    key = ("profile", user.pk)
    if cache is not None and key in cache:
        return cache[key]
    person = (
        Person.objects.filter(
            user_id=user.pk,
            user__is_active=True,
            is_active=True,
            is_external=False,
        )
        .select_related("user")
        .first()
    )
    if cache is not None:
        cache[key] = person
    return person


def is_team_member(user):
    if is_superuser(user):
        return True

    return get_active_person_profile(user) is not None


def _profile_has_role(person, role_name):
    return role_name.casefold() in _role_names(person)


def _profile_has_coordinator_role(person):
    return any(map(roles.is_coordinator_role_name, _role_names(person)))


def coordinates_everything(user):
    """Superuser, rola „Koordynator” (bez działu) albo dawny dostęp koordynatora – pełny zakres obowiązków."""
    if is_superuser(user):
        return True
    person = get_active_person_profile(user)
    return bool(person and (person.legacy_coordinator_access or roles.COORDINATOR.casefold() in _role_names(person)))


def is_coordinator(user):
    if is_superuser(user):
        return True

    person = get_active_person_profile(user)

    if person is None:
        return False

    return bool(
        person.legacy_coordinator_access or _profile_has_coordinator_role(person)
    )


def has_role(user, role_name):
    """Sprawdza rolę osoby, uwzględniając wyłączenie konta i profilu."""
    if not isinstance(role_name, str):
        return False

    role_name = role_name.strip()

    if not role_name:
        return False

    if is_superuser(user):
        return True

    person = get_active_person_profile(user)

    if person is None:
        return False

    if (
        roles.is_general_coordinator_name(role_name)
        and (
            person.legacy_coordinator_access or _profile_has_coordinator_role(person)
        )
    ):
        return True

    return _profile_has_role(person, role_name)


def can_use_recruitment_mailbox(user):
    return is_superuser(user) or has_role(user, roles.RECRUITMENT_COORDINATOR)


def require_recruitment_mailbox(user):
    if not can_use_recruitment_mailbox(user):
        raise PermissionDenied(f"Pobieranie próbek wymaga roli {roles.RECRUITMENT_COORDINATOR} lub superusera.")


def can_view_author_data(user):
    return is_superuser(user)


def can_import_reviews(user):
    # Import zawiera dane osobowe autorów oraz ostrzeżenia o czarnej liście.
    return is_superuser(user)


def can_self_assign_reviews(user):
    from people.leave_access import is_on_leave
    return has_role(user, roles.REVIEWER) and not is_on_leave(user)


def can_mark_review_for_decision(user):
    return is_superuser(user) or has_role(user, roles.REVIEW_COORDINATOR)


def can_manage_reviews(user):
    return is_coordinator(user)


def can_manage_review_files(user):
    """Folder access does not grant access to author personal data."""
    return is_coordinator(user)


def can_view_illustrations(user):
    return is_coordinator(user) or has_role(user, roles.ILLUSTRATOR)


def can_view_audiobooks(user):
    """Jak ilustracje: koordynatorzy oraz osoby z rolą audio (lektorów i montażystów prowadzi spis, nie konta)."""
    return is_coordinator(user) or any(has_role(user, role) for role in roles.AUDIOBOOK_ROLES)


def can_view_post_layout(user):
    return is_coordinator(user) or has_role(user, roles.POST_LAYOUT_PROOFREADER)


def can_view_audio_proofreading(user):
    return is_coordinator(user) or has_role(user, roles.AUDIOBOOK_PROOFREADER)


def can_view_social_media(user):
    """Zakładka Social Media: cały zespół (odczyt Opowiadań i Innych, dodawanie cytatów)."""
    return is_team_member(user)


def can_manage_social_media(user):
    """Edycja Opowiadań i Innych oraz statusów cytatów: superuser, koordynator działu i rola „Social Media”."""
    return is_coordinator(user) or has_role(user, roles.SOCIAL_MEDIA)


def require_post_layout(user):
    if not can_view_post_layout(user):
        raise PermissionDenied(f"Dostęp wymaga uprawnień koordynatora lub roli {roles.POST_LAYOUT_PROOFREADER}.")


def can_manage_vacation(user, vacation):
    if not is_active_user(user) or vacation is None:
        return False

    if is_coordinator(user):
        return True

    person = get_active_person_profile(user)

    return bool(
        person is not None
        and vacation.person_id == person.pk
    )


def require_team_member(user):
    if not is_team_member(user):
        raise PermissionDenied(
            "Dostęp wymaga aktywnego konta i przynależności do zespołu."
        )


def can_copy_emails(user):
    """Koperta w nagłówku (kopiowanie adresów e-mail z tabel strony): koordynatorzy i superuser.

    Kopiuje tylko adresy z tabel, które użytkownik i tak widzi (wszystkie strony listy według jego filtrów),
    oraz nadawców z nagłówków skrzynki, którą może przeglądać – nie rozszerza dostępu do danych."""
    return is_coordinator(user)


def require_coordinator(user):
    if not is_coordinator(user):
        raise PermissionDenied(
            "Ta operacja jest dostępna wyłącznie dla koordynatora."
        )


def require_superuser(user):
    if not is_superuser(user):
        raise PermissionDenied(
            "Ta operacja jest dostępna wyłącznie dla superusera."
        )


def _permission_decorator(check):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in {"GET", "HEAD"}:
                with read_access_scope():
                    check(request.user)
                    return view(request, *args, **kwargs)
            check(request.user)
            return view(request, *args, **kwargs)

        wrapped.permission_check = check
        return wrapped

    return decorator


# Stosuj pod @login_required, aby niezalogowany użytkownik otrzymał
# przekierowanie do logowania, a zalogowany bez dostępu odpowiedź 403.
team_member_required = _permission_decorator(require_team_member)
coordinator_required = _permission_decorator(require_coordinator)
superuser_required = _permission_decorator(require_superuser)
recruitment_mailbox_required = _permission_decorator(require_recruitment_mailbox)
post_layout_required = _permission_decorator(require_post_layout)


def is_reviewer(user):
    person = get_active_person_profile(user)
    return bool(person and _profile_has_role(person, roles.REVIEWER))


def is_reviewer_only(user):
    person = get_active_person_profile(user)
    if not person or user.is_superuser or is_coordinator(user) or not is_reviewer(user):
        return False
    names = _role_names(person)
    return {name.casefold().strip() for name in names} == {roles.REVIEWER.casefold()}


def can_view_my_reviews(user):
    if is_superuser(user):
        return True  # superuser widzi wszystko, także bez profilu osoby (pusta lista przydziałów)
    if not is_team_member(user):
        return False
    if is_reviewer(user):
        return True
    from django.db.models import Q
    from texts.models import ReviewAssignment
    return ReviewAssignment.objects.filter(
        Q(user=user) | Q(historical_person__user=user),
    ).exists()


def can_browse_reviews(user):
    """Lista Recenzje i sekcja Zgłoszenia: recenzenci, koordynatorzy i administratorzy.

    Pozostali członkowie zespołu otwierają pojedyncze recenzje z podglądu tekstu.
    """
    return is_coordinator(user) or (is_team_member(user) and is_reviewer(user))


def can_view_review_archive(user):
    # Decided submissions are visible to the whole team.
    return is_team_member(user)


def can_view_archived_review_authors(user):
    return is_superuser(user)
