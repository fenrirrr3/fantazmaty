from core.translation_scope import ordinary
from texts.models import Anthology, Text
from texts.production import active_production_texts

from .models import Illustration


def illustration_texts_queryset(using='default'):
    """Scope for new illustration links, including published anthologies."""
    return ordinary(Text.objects.using(using)).filter(anthology__has_illustrations=True)


def required_texts_queryset(using='default'):
    return active_production_texts(illustration_texts_queryset(using)).filter(
        anthology__status=Anthology.Status.IN_PREPARATION,
        anthology__has_illustrations=True,
    )


def sync_required_illustrations(anthology=None, using=None):
    using = using or (anthology._state.db if anthology is not None else None) or 'default'
    texts = required_texts_queryset(using)

    if anthology is not None:
        texts = texts.filter(
            anthology=anthology,
        )

    existing_text_ids = set(
        ordinary(Illustration.objects.using(using))
        .filter(
            text_id__in=texts.values_list(
                "pk",
                flat=True,
            )
        )
        .values_list(
            "text_id",
            flat=True,
        )
    )

    missing_illustrations = [
        Illustration(
            text=text,
            status=Illustration.Status.UNASSIGNED,
        )
        for text in texts
        if text.pk not in existing_text_ids
    ]

    if missing_illustrations:
        ordinary(Illustration.objects.using(using)).bulk_create(
            missing_illustrations,
            ignore_conflicts=True,
        )

    return len(missing_illustrations)