"""Kaskadowe fasety: AND między polami, OR między wartościami pola.

Opcje pola uwzględniają pozostałe filtry, dzięki czemu można rozszerzyć
wielokrotny wybór bez wcześniejszego usuwania wybranych statusów.
"""
MAX_DATABASE_ID = 9_223_372_036_854_775_807


def positive_id(value):
    """Identyfikator z parametru GET: dodatnia liczba w zakresie bazy albo None."""
    value = (value or "").strip()
    if (
        not value
        or len(value) > 19
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    result = int(value)
    return result if 1 <= result <= MAX_DATABASE_ID else None


def facet_queryset(queryset, dimensions):
    def apply(exclude=None):
        result = queryset
        for name, (field, values) in dimensions.items():
            if name != exclude and values:
                result = result.filter(**{f'{field}__in': values})
        return result
    options = {
        name: set(apply(name).order_by().values_list(field, flat=True).distinct()) | set(values)
        for name, (field, values) in dimensions.items()
    }
    return apply(), options
