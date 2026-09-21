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


def _role_names(person):
    cache = _read_access.get()
    key = ("roles", person.pk)
    if cache is not None and key in cache:
        return cache[key]
    names = {name.casefold() for name in person.roles.values_list("name", flat=True)}
    names.update(name.casefold() for name in person.user.groups.values_list("name", flat=True))
    if cache is not None:
        cache[key] = names
    return names


COORDINATOR_ROLE = "Koordynator"
COORDINATOR_ROLE_PREFIX = f"{COORDINATOR_ROLE} "
REVIEWER_ROLE = "Recenzent"


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
    return any(name == "koordynator" or name.startswith("koordynator ")
               for name in _role_names(person))


def is_coordinator(user):
    if is_superuser(user):
        return True

    person = get_active_person_profile(user)

    if person is None:
        return False

    return bool(
        person.is_coordinator
        or _profile_has_coordinator_role(person)
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
        role_name.casefold() == COORDINATOR_ROLE.casefold()
        and (
            person.is_coordinator
            or _profile_has_coordinator_role(person)
        )
    ):
        return True

    return _profile_has_role(person, role_name)


def has_coordinator_access(user):
    """Zgodność z dotychczasową nazwą używaną w widokach."""
    return is_coordinator(user)


def belongs_to_group(user, group_name):
    """Zgodność z dotychczasową nazwą sprawdzania roli."""
    return has_role(user, group_name)


def can_view_author_data(user):
    return is_superuser(user)


def can_view_review_author(user, review=None):
    # Uprawnienie nie zależy od statusu recenzji ani przydziału.
    # Koordynator nie uzyskuje dostępu do tożsamości autora.
    return can_view_author_data(user)


def can_manage_authors(user):
    return is_superuser(user)


def can_import_reviews(user):
    # Import zawiera dane osobowe autorów oraz ostrzeżenia o czarnej liście.
    return is_superuser(user)


def can_self_assign_reviews(user):
    from people.leave_access import is_on_leave
    return has_role(user, REVIEWER_ROLE) and not is_on_leave(user)


def can_manage_reviews(user):
    return is_coordinator(user)






def can_view_illustrations(user):
    return is_coordinator(user) or has_role(user, "Ilustrator")


def can_view_reports(user):
    return is_coordinator(user)


def can_restart_workflow(user):
    return is_superuser(user)


def can_perform_bulk_actions(user):
    # Operacje zbiorcze zachowują dotychczasowe ograniczenie do superusera.
    return is_superuser(user)


def can_export_author_data(user):
    return is_superuser(user)






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


def require_author_data_access(user):
    if not can_view_author_data(user):
        raise PermissionDenied(
            "Dane autora są dostępne wyłącznie dla superusera."
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
author_data_required = _permission_decorator(require_author_data_access)


def is_reviewer(user):
    person = get_active_person_profile(user)
    return bool(person and _profile_has_role(person, REVIEWER_ROLE))


def is_reviewer_only(user):
    person = get_active_person_profile(user)
    if not person or user.is_superuser or is_coordinator(user) or not is_reviewer(user):
        return False
    names = _role_names(person)
    return {name.casefold().strip() for name in names} == {"recenzent"}


def can_view_my_reviews(user):
    if not is_team_member(user):
        return False
    if is_reviewer(user):
        return True
    from django.db.models import Q
    from texts.models import ReviewAssignment
    return ReviewAssignment.objects.filter(
        Q(user=user) | Q(historical_person__user=user), review__old_reviews=True,
    ).exists()
