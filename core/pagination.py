"""Wspólna paginacja list aplikacji.

Przed przekazaniem QuerySetu należy zastosować filtrowanie i sortowanie.
Funkcje zwracają Page zgodne z dotychczasowymi szablonami.
"""

from django.core.paginator import Paginator
from django.db.models import QuerySet


DEFAULT_PAGE_SIZE = 25
ALLOWED_PAGE_SIZES = frozenset({25, 50, 100, 250, 500})


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


def get_page_size(request, default=DEFAULT_PAGE_SIZE, *, size_param='page_size'):
    if default not in ALLOWED_PAGE_SIZES:
        raise ValueError(
            "Domyślny rozmiar strony musi należeć "
            "do ALLOWED_PAGE_SIZES."
        )

    # Filters and page numbers do not change the preference's identity.
    # Tabs and independent paginators on one view retain separate settings.
    session = getattr(request, 'session', None)
    key = f'{request.path}|{size_param}|{request.GET.get("tab", "")}'
    preferences = dict(session.get('cms_page_sizes', {})) if session is not None else {}
    saved = _positive_integer(preferences.get(key), default)
    if saved not in ALLOWED_PAGE_SIZES:
        saved = default
    requested = _positive_integer(request.GET.get(size_param), None)
    if requested not in ALLOWED_PAGE_SIZES:
        return saved
    if session is not None and preferences.get(key) != requested:
        preferences.pop(key, None)
        preferences[key] = requested
        # Detail views can have many different URLs; keep the session bounded.
        session['cms_page_sizes'] = dict(list(preferences.items())[-100:])
    return requested


def _page_url(parameters, page_number, page_param='page', anchor=''):
    parameters = parameters.copy()
    parameters[page_param] = str(page_number)
    return f"?{parameters.urlencode()}{anchor}"


def paginate_items(request, items, default=DEFAULT_PAGE_SIZE, *, page_param='page', size_param='page_size', anchor=''):
    """Stronicuje QuerySet lub sekwencję bez wczytywania całego QuerySetu."""
    from core.table_sorting import prepare_table_sort
    items, sort_columns = prepare_table_sort(request, items)
    page_size = get_page_size(request, default=default, size_param=size_param)
    page_number = _positive_integer(
        request.GET.get(page_param),
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
    parameters.pop(page_param, None)
    parameters[size_param] = str(page_size)

    page_obj.sort_columns = sort_columns
    page_obj.selected_page_size = page_size
    page_obj.allowed_page_sizes = tuple(sorted(ALLOWED_PAGE_SIZES))
    page_obj.page_param = page_param
    page_obj.size_param = size_param
    page_obj.anchor = anchor
    page_obj.size_options = []
    for size in sorted(ALLOWED_PAGE_SIZES):
        size_parameters = parameters.copy()
        size_parameters[size_param] = str(size)
        page_obj.size_options.append({'size': size, 'url': _page_url(size_parameters, 1, page_param, anchor)})
    page_obj.query_string = parameters.urlencode()

    # QueryDict zachowuje wielokrotne wartości, np. roles=1&roles=2.
    # Szablony powinny używać zwykłego autoescape, bez filtra safe.
    page_obj.first_url = _page_url(parameters, 1, page_param, anchor)
    page_obj.last_url = _page_url(parameters, paginator.num_pages, page_param, anchor)
    page_obj.previous_url = (
        _page_url(parameters, page_obj.previous_page_number(), page_param, anchor)
        if page_obj.has_previous()
        else None
    )
    page_obj.next_url = (
        _page_url(parameters, page_obj.next_page_number(), page_param, anchor)
        if page_obj.has_next()
        else None
    )

    return page_obj


def paginate_queryset(request, queryset, default=DEFAULT_PAGE_SIZE):
    """Zachowuje interfejs używany przez moduł ilustracji."""
    return paginate_items(request, queryset, default=default)
