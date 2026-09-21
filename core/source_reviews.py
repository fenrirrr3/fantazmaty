"""Explicit review links; title matching supplies suggestions only."""
from django.core.exceptions import ValidationError
from django.db import transaction
from core.permissions import require_superuser
from texts.models import Review, Text


def linkable_reviews(text):
    from django.db.models import Q
    return Review.objects.filter(status=Review.Status.ACCEPTED, anthology_id=text.anthology_id).filter(
        Q(copied_text__isnull=True) | Q(copied_text_id=text.pk)
    ).order_by('title','pk')


def suggested_review_ids(text):
    authors = set(text.authors.values_list('pk',flat=True))
    if not authors:
        return []
    candidates = linkable_reviews(text).filter(title__iexact=text.title.strip(),author_id__in=authors).prefetch_related('coauthors')
    return [review.pk for review in candidates if {review.author_id} | {a.pk for a in review.coauthors.all()} == authors]


@transaction.atomic
def link_source_review(*, user, text_id, review_id):
    require_superuser(user)
    text = Text.objects.select_for_update().get(pk=text_id)
    existing = Review.objects.filter(copied_text_id=text.pk).values_list('pk',flat=True).first()
    ids = {review_id}
    if existing:
        ids.add(existing)
    locked = {row.pk: row for row in Review.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    chosen = locked.get(review_id)
    if not chosen or chosen.status != Review.Status.ACCEPTED or chosen.anthology_id != text.anthology_id:
        raise ValidationError('Wybierz przyjęte zgłoszenie z tej samej antologii. Dane mogły się zmienić.')
    if chosen.copied_text_id not in (None,text.pk):
        raise ValidationError('To zgłoszenie jest już powiązane z innym tekstem.')
    if existing and existing != chosen.pk:
        previous = locked[existing]
        previous.copied_text = None
        previous.save(update_fields=['copied_text'])
    if chosen.copied_text_id != text.pk:
        chosen.copied_text = text
        chosen.save(update_fields=['copied_text'])
        from core.edit_versions import bump
        bump('texts.text',text.pk,text._state.db)
    return chosen
