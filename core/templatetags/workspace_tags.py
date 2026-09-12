from django import template
from core.permissions import can_view_illustrations
from core.permissions import is_coordinator
register = template.Library()


@register.simple_tag
def illustrations_allowed(user):
    return can_view_illustrations(user)


@register.simple_tag
def coordinator_allowed(user):
    return is_coordinator(user)


@register.simple_tag
def reviewer_allowed(user):
    from core.permissions import is_reviewer
    return is_reviewer(user)
