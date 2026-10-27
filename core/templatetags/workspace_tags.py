from django import template

from core import permissions

register = template.Library()

# Nazwa sprawdzenia w szablonie → reguła dostępu ({% allowed "audiobooks" user as show_audiobooks %}).
ALLOWED = {
    "post_layout": "can_view_post_layout", "audiobooks": "can_view_audiobooks",
    "social_media": "can_view_social_media", "audio_proofreading": "can_view_audio_proofreading",
    "illustrations": "can_view_illustrations", "coordinator": "is_coordinator", "email_copy": "can_copy_emails",
    "my_reviews": "can_view_my_reviews", "review_list": "can_browse_reviews",
    "recruitment_mailbox": "can_use_recruitment_mailbox", "table_csv": "is_superuser",
}


@register.simple_tag
def allowed(name, user):
    return getattr(permissions, ALLOWED[name])(user)


@register.inclusion_tag("core/includes/header_search.html")
def header_search(user):
    from core.forms import GlobalSearchForm
    return {"header_search_form": GlobalSearchForm(user=user, auto_id="header_%s")
            if permissions.is_team_member(user) else None}


@register.simple_tag
def account_person(user):
    # Ten sam rekord co relacja OneToOne; brak profilu daje None.
    if not user.is_authenticated:
        return None
    return getattr(user, "person_profile", None)


@register.simple_tag(takes_context=True)
def text_view_url(context, view):
    params = context["request"].GET.copy()
    params["view"] = view
    params.pop("page", None)
    return "?" + params.urlencode()


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
