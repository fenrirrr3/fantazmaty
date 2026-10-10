from urllib.parse import urlsplit

from django.core.exceptions import ValidationError


def validate_mega_url(value):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == 'https'
            and parsed.hostname in ('mega.nz', 'www.mega.nz', 'mega.co.nz', 'www.mega.co.nz')
            and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError('Wklej link HTTPS z mega.nz lub mega.co.nz.')
