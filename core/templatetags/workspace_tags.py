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
    from core.permissions import can_view_my_reviews
    return can_view_my_reviews(user)


@register.simple_tag
def account_person(user):
    from people.models import Person
    if not user.is_authenticated:
        return None
    person = Person.objects.filter(user=user).first()
    if person is not None:
        return person
    return None
