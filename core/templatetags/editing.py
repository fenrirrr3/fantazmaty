from django import template
from django.utils.html import format_html

register = template.Library()

@register.simple_tag(takes_context=True)
def edit_version(context):
    token = getattr(context.get('request'), 'edit_version_token', '')
    return format_html('<input type="hidden" name="_edit_version" value="{}">', token) if token else ''
