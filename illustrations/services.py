from core.translation_scope import ordinary
from texts.models import Anthology, Text
from texts.production import active_production_texts

from .models import Illustration


def required_texts_queryset():
    return active_production_texts(ordinary(Text.objects)).filter(
        anthology__status=Anthology.Status.IN_PREPARATION,
        anthology__has_illustrations=True,
    )


def sync_required_illustrations(anthology=None):
    texts = required_texts_queryset()

    if anthology is not None:
        texts = texts.filter(
            anthology=anthology,
        )

    existing_text_ids = set(
        ordinary(Illustration.objects)
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
        ordinary(Illustration.objects).bulk_create(
            missing_illustrations,
            ignore_conflicts=True,
        )

    return len(missing_illustrations)