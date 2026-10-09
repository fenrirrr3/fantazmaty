from django import template
from core.permissions import can_view_illustrations
from core.permissions import is_coordinator
register = template.Library()


@register.simple_tag
def post_layout_allowed(user):
    from core.permissions import can_view_post_layout
    return can_view_post_layout(user)


@register.inclusion_tag("core/includes/header_search.html")
def header_search(user):
    from core.forms import GlobalSearchForm
    from core.permissions import is_team_member
    return {"header_search_form": GlobalSearchForm(user=user, auto_id="header_%s")
            if is_team_member(user) else None}


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


@register.simple_tag(takes_context=True)
def text_view_url(context, view):
    params = context["request"].GET.copy()
    params["view"] = view
    params.pop("page", None)
    return "?" + params.urlencode()


@register.simple_tag
def recruitment_mailbox_allowed(user):
    from core.permissions import can_use_recruitment_mailbox
    return can_use_recruitment_mailbox(user)


@register.simple_tag
def recruitment_notified_token(user, record):
    from core.views.recruitment_notified import notified_token
    return notified_token(user, record)


@register.filter
def role_palette_attr(code, label=''):
    from core.palettes import palette_attr, role_palette
    return palette_attr(role_palette(code, label))


@register.filter
def stage_palette_attr(code, label=''):
    from core.palettes import palette_attr, stage_palette
    return palette_attr(stage_palette(code, label))


@register.filter
def label_palette_attr(label):
    from core.palettes import label_palette, palette_attr
    return palette_attr(label_palette(label))
