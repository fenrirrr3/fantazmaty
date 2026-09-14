"""Reuse stored contact numbers without guessing an author's identity."""
from django.db.models import Q
from texts.models import Extract, Review


def stored_author_phone(author):
    matches = Q(author=author)
    if author.email:
        matches |= Q(author__isnull=True, email__iexact=author.email)
    phones = Review.objects.filter(matches).exclude(phone_number='').order_by('-created_at', '-pk').values_list('phone_number', flat=True)
    for phone in phones:
        if phone.strip():
            return phone.strip()
    for phone in Extract.objects.filter(author=author).exclude(phone_number='').order_by('-pk').values_list('phone_number', flat=True):
        if phone.strip():
            return phone.strip()
    return ''
