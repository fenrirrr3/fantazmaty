"""Explicit review links; title matching supplies suggestions only."""
from django.core.exceptions import ValidationError
from django.db import transaction
from core.permissions import require_superuser
from texts.models import Review, Text


def linkable_reviews(text):
    from django.db.models import Q
    return Review.objects.filter(anthology_id=text.anthology_id).filter(
        Q(status=Review.Status.ACCEPTED) | Q(old_reviews=True)
    ).filter(
        Q(copied_text__isnull=True) | Q(copied_text_id=text.pk)
    ).order_by('title','pk')


def suggested_review_ids(text):
    authors = set(text.authors.values_list('pk',flat=True))
    if not authors:
        return []
    candidates = linkable_reviews(text).filter(title__iexact=text.title.strip(),author_id__in=authors).prefetch_related('coauthors')
    return [review.pk for review in candidates if {review.author_id} | {a.pk for a in review.coauthors.all()} == authors]


def validate_source_review_link(text, review, *, coauthors=None, confirm_mismatch=False):
    """Shared, non-mutating validation for admin and explicit source links."""
    if not review or (review.status != Review.Status.ACCEPTED and not review.old_reviews) or review.anthology_id != text.anthology_id:
        raise ValidationError('Wybierz przyjęte lub archiwalne zgłoszenie z tej samej antologii. Dane mogły się zmienić.')
    from texts.services import normalize_author_name
    text_authors = list(text.authors.all())
    text_author_ids = {author.pk for author in text_authors}
    review_author_ids = {a.pk for a in coauthors} if coauthors is not None else set(review.coauthors.values_list('pk', flat=True))
    if review.author_id:
        review_author_ids.add(review.author_id)
        authors_mismatch = review_author_ids != text_author_ids
    else:
        # An unlinked submission may still identify an existing author. Profile
        # IDs remain authoritative whenever the submission already has a link.
        expected_name = normalize_author_name(f'{review.author_first_name} {review.author_last_name}')
        possible_primary = [author for author in text_authors
            if normalize_author_name(str(author)) == expected_name
            and (not review.email or (author.email or '').strip().casefold() == review.email.strip().casefold())]
        authors_mismatch = not any(review_author_ids | {author.pk} == text_author_ids
                                  for author in possible_primary)
    mismatch = review.status != Review.Status.ACCEPTED or review.title.strip().casefold() != text.title.strip().casefold() or authors_mismatch
    if mismatch and confirm_mismatch is not True:
        raise ValidationError('Tytuł, autorzy lub decyzja zgłoszenia nie zgadzają się z przyjętym tekstem. Sprawdź dane i zaznacz potwierdzenie rozbieżności.')
    if review.copied_text_id not in (None,text.pk):
        raise ValidationError('To zgłoszenie jest już powiązane z innym tekstem.')


@transaction.atomic
def link_source_review(*, user, text_id, review_id, confirm_mismatch=False):
    require_superuser(user)
    text = Text.objects.select_for_update().get(pk=text_id)
    existing = Review.objects.filter(copied_text_id=text.pk).values_list('pk',flat=True).first()
    ids = {review_id}
    if existing:
        ids.add(existing)
    locked = {row.pk: row for row in Review.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    chosen = locked.get(review_id)
    validate_source_review_link(text, chosen, confirm_mismatch=confirm_mismatch)
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
