"""Kolejność ról w filtrze zespołu; dodatkowe role zachowujemy na końcu."""
from django.db.models import Case, IntegerField, Value, When
from .models import Role
from .roles import TEAM_ROLE_ORDER


def ordered_team_roles():
    return Role.objects.annotate(
        team_order=Case(
            *(When(name__iexact=name, then=Value(index)) for index, name in enumerate(TEAM_ROLE_ORDER)),
            default=Value(len(TEAM_ROLE_ORDER)), output_field=IntegerField(),
        )
    ).order_by("team_order", "name", "pk")
