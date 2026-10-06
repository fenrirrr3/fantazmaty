"""One persistent translation record per text; flags change visibility, not history."""
from django.db import transaction
from django.core.exceptions import ValidationError
from .models import Text, TextTranslation, ForeignAuthor


def sync_translations(*, anthology_id=None, text_id=None, using='default'):
    texts = Text.objects.using(using).filter(anthology__is_translated=True, anthology__is_novel=False)
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


def ordinary_author_links(anthology, using='default'):
    """Recover only explicit legacy identities, never guess foreign names."""
    from authors.models import Author
    records = TextTranslation.objects.using(using).filter(
        text__anthology_id=anthology.pk,
    ).prefetch_related('foreign_authors')
    links = []
    known = set(Author.objects.using(using).values_list('pk', flat=True))
    for record in records:
        profiles = list(record.foreign_authors.all())
        if any(profile.legacy_author_id not in known for profile in profiles):
            raise ValidationError({'is_translated': (
                'Nie można wyłączyć tłumaczenia: część autorów zagranicznych nie ma '
                'powiązania ze zwykłym autorem. Najpierw ustal mapowanie autorów; nic nie zapisano.'
            )})
        links.append((record.text_id, [profile.legacy_author_id for profile in profiles]))
    return links


def restore_ordinary_authors(anthology, using='default'):
    with transaction.atomic(using=using):
        for text_id, author_ids in ordinary_author_links(anthology, using):
            Text.objects.using(using).get(pk=text_id).authors.add(*author_ids)
