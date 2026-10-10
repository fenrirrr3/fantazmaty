from urllib.parse import urlsplit

from django.core.exceptions import ValidationError


def _media_url(value, hosts, label):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname in hosts
                 and not parsed.username and not parsed.password and parsed.port in (None, 80, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError(f'Wklej prawidłowy link do {label}.')


def validate_youtube_url(value):
    _media_url(value, ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'), 'YouTube')


def validate_hearthis_url(value):
    _media_url(value, ('hearthis.at', 'www.hearthis.at'), 'HearThis')


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
