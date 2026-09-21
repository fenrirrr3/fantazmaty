"""Explicit revocation removes all coordinator sources without deleting other roles."""
from contextvars import ContextVar
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from .models import Person

revoking = ContextVar('revoking_coordinator', default=False)


def coordinator_query():
    return Q(name__iexact='Koordynator') | Q(name__istartswith='Koordynator ')


@transaction.atomic
def revoke_coordinator(person):
    if revoking.get():
        return
    token = revoking.set(True)
    try:
        user = get_user_model().objects.select_for_update().filter(pk=person.user_id).first()
        current = Person.objects.select_for_update().get(pk=person.pk)
        current.roles.remove(*current.roles.filter(coordinator_query()))
        Person.objects.filter(pk=current.pk).update(is_coordinator=False)
        from core.edit_versions import bump
        bump('people.person', current.pk, current._state.db)
        person.is_coordinator = False
        if user:
            user.groups.remove(*user.groups.filter(coordinator_query()))
            # Preserve independent admin permissions and superusers.
            if not user.is_superuser and not user.user_permissions.exists() and not user.groups.filter(permissions__isnull=False).exists():
                get_user_model().objects.filter(pk=user.pk).update(is_staff=False)
                bump(user._meta.label_lower, user.pk, user._state.db)
    finally:
        revoking.reset(token)
