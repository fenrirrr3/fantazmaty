from texts.models import Anthology, Text

from .models import Illustration


def required_texts_queryset():
    return Text.objects.filter(
        anthology__status=Anthology.Status.UNPUBLISHED,
        anthology__has_illustrations=True,
    )


def sync_required_illustrations(anthology=None):
    texts = required_texts_queryset()

    if anthology is not None:
        texts = texts.filter(
            anthology=anthology,
        )

    existing_text_ids = set(
        Illustration.objects
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
        Illustration.objects.bulk_create(
            missing_illustrations,
            ignore_conflicts=True,
        )

    return len(missing_illustrations)