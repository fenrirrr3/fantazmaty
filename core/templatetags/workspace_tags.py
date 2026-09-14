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
    email = (user.email or user.get_username()).strip()
    matches = list(Person.objects.filter(email__iexact=email, is_active=True)[:2]) if email else []
    return matches[0] if len(matches) == 1 else None
