"""Shared Polish alphabet ordering for Python, SQL and the equivalent browser helper."""
from django.db import connections
from django.db.models import F, Value
from django.db.models.functions import Collate, Lower, Replace, Trim

REPLACEMENTS = (('ą','a~'),('ć','c~'),('ę','e~'),('ł','l~'),('ń','n~'),('ó','o~'),('ś','s~'),('ź','z~'),('ż','z~~'))

def text_key(value):
    value=str(value).strip().lower()
    for source,target in REPLACEMENTS:
        value=value.replace(source,target)
    return value

def sql_text_key(field, using):
    expression=Trim(Lower(F(field)))
    for source,target in REPLACEMENTS:
        expression=Replace(expression,Value(source),Value(target))
    return Collate(expression, 'utf8mb4_bin' if connections[using].vendor=='mysql' else 'BINARY' if connections[using].vendor=='sqlite' else 'C')
