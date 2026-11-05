"""Znaczniki strony Programy: opis reguły Odkurzacza (zwijany „?” przy regule)."""
from django import template

from core.services.odkurzacz import RULE_DESCRIPTIONS

register = template.Library()


@register.filter
def rule_description(key):
    """Opis reguły o kluczu `key` albo '' (reguła bez opisu)."""
    return RULE_DESCRIPTIONS.get(str(key), '')
