"""One persistent translation record per text; flags change visibility, not history."""
from .models import Text, TextTranslation


def sync_translations(*, anthology_id=None, text_id=None, using='default'):
    texts = Text.objects.using(using).filter(anthology__is_translated=True)
    if anthology_id is not None:
        texts = texts.filter(anthology_id=anthology_id)
    if text_id is not None:
        texts = texts.filter(pk=text_id)
    missing = texts.filter(translation__isnull=True).values_list('pk', flat=True)
    return TextTranslation.objects.using(using).bulk_create(
        [TextTranslation(text_id=pk) for pk in missing], ignore_conflicts=True)
