"""Public signatures: a pseudonym replaces the legal name outside admin."""
from django.db.models import Case, When, Q, Value, CharField, F
from django.db.models.functions import Concat, Trim


def public_name(prefix=''):
    return Case(
        When(**{prefix + 'pseudonym': ''}, then=Trim(Concat(
            F(prefix + 'first_name'), Value(' '), F(prefix + 'last_name')))),
        default=Trim(F(prefix + 'pseudonym')), output_field=CharField(),
    )


def name_matches(term, prefix='', *, email=False):
    names = Q(**{prefix + 'first_name__plcontains': term}) | Q(**{prefix + 'last_name__plcontains': term})
    result = Q(**{prefix + 'pseudonym__plcontains': term}) | (Q(**{prefix + 'pseudonym': ''}) & names)
    if email:
        result |= Q(**{prefix + 'email__plcontains': term})
    return result


def review_name_matches(term):
    # Linked authors are the displayed source; old copied identity fields are not.
    linked = name_matches(term, 'author__', email=True) | name_matches(term, 'coauthors__')
    fallback = (Q(author_pseudonym__plcontains=term) |
                (Q(author_pseudonym='') & (Q(author_first_name__plcontains=term) | Q(author_last_name__plcontains=term))))
    return linked | (Q(author__isnull=True, coauthors__isnull=True) & fallback) | Q(email__plcontains=term)
