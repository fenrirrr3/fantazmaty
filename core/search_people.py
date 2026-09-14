"""Rank name matches before contact matches; roles are a separate filter."""
from django.db.models import Case, IntegerField, Q, Value, When


def terms_match(query, fields):
    result = Q()
    for term in query.split():
        part = Q()
        for field in fields:
            part |= Q(**{f"{field}__plcontains": term})
        result &= part
    return result


def rank_people(queryset, query):
    if not query.strip():
        return queryset.order_by('last_name', 'first_name', 'pk')
    names = terms_match(query, ('first_name', 'last_name'))
    matches = terms_match(query, ('first_name', 'last_name', 'email'))
    return queryset.filter(matches).annotate(
        name_rank=Case(When(names, then=Value(0)), default=Value(1), output_field=IntegerField())
    ).order_by('name_rank', 'last_name', 'first_name', 'pk')
