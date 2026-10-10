"""Explicit scope for ordinary CMS lists; admin and translation views remain unfiltered."""
PATHS = {
    'texts.anthology': ('is_translated',),
    'texts.text': ('anthology__is_translated',),
    'texts.review': ('anthology__is_translated', 'copied_text__anthology__is_translated'),
    'texts.reviewassignment': ('review__anthology__is_translated', 'review__copied_text__anthology__is_translated'),
    'texts.anthologytask': ('anthology__is_translated',),
    'workflow.workflowstage': ('text__anthology__is_translated',),
    'workflow.workflowroleassignment': ('text__anthology__is_translated',),
    'workflow.workflowrepetition': ('text__anthology__is_translated',),
    'workflow.workflowhandoff': ('text__anthology__is_translated',),
    'illustrations.illustration': ('text__anthology__is_translated',),
    'core.anthologycorrection': ('anthology__is_translated',),
    'core.workflowevent': ('text__anthology__is_translated',),
}


def ordinary(queryset, *, include_abandoned=False):
    for path in PATHS.get(queryset.model._meta.label_lower, ()):
        queryset = queryset.exclude(**{path: True})
    return queryset if include_abandoned else non_abandoned(queryset)


def non_abandoned(queryset):
    """Frontend scope only. Never alter default managers or historical records."""
    for path in PATHS.get(queryset.model._meta.label_lower, ()):
        queryset = queryset.exclude(**{path.replace('is_translated', 'status'): 'abandoned'})
    return queryset
