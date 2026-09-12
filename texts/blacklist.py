from datetime import timedelta
from secrets import randbelow

from django.utils import timezone


def apply_blacklist(review):
    """Wywoływane przy tworzeniu po potwierdzeniu ostrzeżeń formularza."""
    from .services import find_matching_authors
    authors = find_matching_authors(
        author=review.author if review.author_id else None,
        email=review.email,
    )
    blacklisted = any(author.is_blacklisted for author in authors)
    if review.author_id:
        blacklisted = blacklisted or review.author.is_blacklisted
    if blacklisted:
        review.is_hidden = True
        review.status = review.Status.REJECTED
        review.decision_at = timezone.localdate() + timedelta(days=14 + randbelow(15))
        review.author_notified_at = None
