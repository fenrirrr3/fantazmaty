"""Wspólna paginacja list aplikacji.

Przed przekazaniem QuerySetu należy zastosować filtrowanie i sortowanie.
Funkcje zwracają Page zgodne z dotychczasowymi szablonami.
"""

from django.core.paginator import Paginator
from django.db.models import QuerySet


DEFAULT_PAGE_SIZE = 25
ALLOWED_PAGE_SIZES = frozenset({25, 50, 100})


def _positive_integer(value, default):
    if value is None:
        return default

    value = str(value).strip()

    if (
        not value
        or len(value) > 19
        or not value.isascii()
        or not value.isdecimal()
    ):
        return default

    number = int(value)
    return number if number > 0 else default


def get_page_size(request, default=DEFAULT_PAGE_SIZE):
    if default not in ALLOWED_PAGE_SIZES:
        raise ValueError(
            "Domyślny rozmiar strony musi należeć "
            "do ALLOWED_PAGE_SIZES."
        )

    page_size = _positive_integer(
        request.GET.get("page_size"),
        default,
    )

    if page_size not in ALLOWED_PAGE_SIZES:
        return default

    return page_size


def _page_url(parameters, page_number):
    parameters = parameters.copy()
    parameters["page"] = str(page_number)
    return f"?{parameters.urlencode()}"


def paginate_items(request, items, default=DEFAULT_PAGE_SIZE):
    """Stronicuje QuerySet lub sekwencję bez wczytywania całego QuerySetu."""
    page_size = get_page_size(request, default=default)
    page_number = _positive_integer(
        request.GET.get("page"),
        default=1,
    )

    if isinstance(items, QuerySet) and not items.ordered:
        items = items.order_by("pk")

    paginator = Paginator(
        items,
        per_page=page_size,
        allow_empty_first_page=True,
    )

    # Nieistniejący dodatni numer wskazuje ostatnią dostępną stronę.
    # Pusty wynik nadal daje poprawny obiekt pierwszej strony.
    page_obj = paginator.get_page(page_number)

    parameters = request.GET.copy()
    parameters.pop("page", None)
    parameters["page_size"] = str(page_size)

    page_obj.selected_page_size = page_size
    page_obj.allowed_page_sizes = tuple(sorted(ALLOWED_PAGE_SIZES))
    page_obj.query_string = parameters.urlencode()

    # QueryDict zachowuje wielokrotne wartości, np. roles=1&roles=2.
    # Szablony powinny używać zwykłego autoescape, bez filtra safe.
    page_obj.first_url = _page_url(parameters, 1)
    page_obj.last_url = _page_url(parameters, paginator.num_pages)
    page_obj.previous_url = (
        _page_url(parameters, page_obj.previous_page_number())
        if page_obj.has_previous()
        else None
    )
    page_obj.next_url = (
        _page_url(parameters, page_obj.next_page_number())
        if page_obj.has_next()
        else None
    )

    return page_obj


def paginate_queryset(request, queryset, default=DEFAULT_PAGE_SIZE):
    """Zachowuje interfejs używany przez moduł ilustracji."""
    return paginate_items(request, queryset, default=default)