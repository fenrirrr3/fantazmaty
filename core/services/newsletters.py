"""Newsletter data is persisted only after a successful submission save."""
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from core.models import NewsletterConsent
from texts.services import normalize_email


def parse_consents(value):
    tokens = {part.casefold() for part in re.split(r'[,\s]+', value.strip()) if part}
    if tokens - {'premierach', 'naborach'}:
        raise ValidationError('Zgody: dozwolone są „premierach”, „naborach”, obie wartości oddzielone przecinkiem lub puste pole.')
    return {'premieres': 'premierach' in tokens, 'recruitment': 'naborach' in tokens}


@transaction.atomic
def record_consents(email, *, premieres=False, recruitment=False):
    email = normalize_email(email)
    if not email:
        return
    consent, _ = NewsletterConsent.objects.select_for_update().get_or_create(email=email)
    # An unchecked option on another submission is not an unsubscribe request.
    consent.premieres = consent.premieres or bool(premieres)
    consent.recruitment = consent.recruitment or bool(recruitment)
    consent.save(update_fields=['premieres', 'recruitment', 'updated_at'])
