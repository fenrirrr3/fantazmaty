"""Kolejność ról w filtrze zespołu; dodatkowe role zachowujemy na końcu."""
from django.db.models import Case, IntegerField, Value, When
from .models import Role

TEAM_ROLE_ORDER = (
    "Recenzent", "Redaktor", "Korektor", "Weryfikator", "Korektor poskładowy",
    "Grafik", "Dźwiękowiec", "Lektor", "Składacz", "Tłumacz", "Koordynator",
    "Koordynator redakcji", "Koordynator audiobooków",
    "Koordynator weryfikacji", "Koordynator ilustracji",
    "Koordynator recenzji", "Koordynator korekty",
    "Koordynator rekrutacji",
)


def ordered_team_roles():
    return Role.objects.annotate(
        team_order=Case(
            *(When(name__iexact=name, then=Value(index)) for index, name in enumerate(TEAM_ROLE_ORDER)),
            default=Value(len(TEAM_ROLE_ORDER)), output_field=IntegerField(),
        )
    ).order_by("team_order", "name", "pk")
