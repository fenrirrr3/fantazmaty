"""Newsletter data is persisted only after a successful submission save."""
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from core.models import NewsletterConsent
from texts.services import normalize_email


# Wartości pola „Newsletter” z formularzy naborów: „tak” – newsletter ogólny, „tylko nabory” – same nabory;
# dawne „premierach” (= ogólny) i „naborach” (= nabory) nadal są rozpoznawane, „nie” to brak zgody.
CONSENT_VALUES = {'tak': 'premieres', 'premierach': 'premieres', 'tylko nabory': 'recruitment',
                  'naborach': 'recruitment', 'nie': None}


def parse_consents(value):
    words = [part.casefold() for part in re.split(r'[,\s]+', value.strip()) if part]
    found = set()
    while words:
        phrase = ' '.join(words[:2])
        if phrase in CONSENT_VALUES:
            words = words[2:]
        elif (phrase := words[0]) in CONSENT_VALUES:
            words = words[1:]
        else:
            raise ValidationError('Zgody: dozwolone są „tak”, „tylko nabory” (dawniej „premierach”, „naborach”), '
                                  'kilka wartości oddzielonych przecinkiem albo puste pole.')
        found.add(CONSENT_VALUES[phrase])
    return {'premieres': 'premieres' in found, 'recruitment': 'recruitment' in found}


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
