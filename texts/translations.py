"""One persistent translation record per text; flags change visibility, not history."""
from django.db import transaction
from .models import Text, TextTranslation, ForeignAuthor


def sync_translations(*, anthology_id=None, text_id=None, using='default'):
    texts = Text.objects.using(using).filter(anthology__is_translated=True)
    if anthology_id is not None:
        texts = texts.filter(anthology_id=anthology_id)
    if text_id is not None:
        texts = texts.filter(pk=text_id)
    missing = texts.filter(translation__isnull=True).values_list('pk', flat=True)
    with transaction.atomic(using=using):
        created = TextTranslation.objects.using(using).bulk_create(
            [TextTranslation(text_id=pk) for pk in missing], ignore_conflicts=True)
        # A newly flagged anthology may already have ordinary author links.
        for text in texts.filter(authors__isnull=False).distinct().prefetch_related('authors'):
            record = TextTranslation.objects.using(using).get(text=text)
            for author in text.authors.all():
                profile, _ = ForeignAuthor.objects.using(using).get_or_create(
                    legacy_author_id=author.pk, defaults={
                        'first_name': author.first_name, 'last_name': author.last_name,
                        'pseudonym': author.pseudonym, 'email': author.email or '',
                        'phone_number': author.phone_number,
                    })
                record.foreign_authors.add(profile)
            text.authors.clear()
        return created
