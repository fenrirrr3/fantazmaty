from urllib.parse import urlsplit
from django.core.exceptions import ValidationError


def validate_drive_url(value):
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme == 'https' and parsed.hostname in ('drive.google.com', 'docs.google.com') and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError('Wklej link HTTPS z drive.google.com lub docs.google.com.')
