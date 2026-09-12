"""Kaskadowe fasety: AND między polami, OR między wartościami pola.

Opcje pola uwzględniają pozostałe filtry, dzięki czemu można rozszerzyć
wielokrotny wybór bez wcześniejszego usuwania wybranych statusów.
"""
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
