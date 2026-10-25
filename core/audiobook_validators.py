"""Walidatory linków: dozwolone domeny (audiobooki, ilustracje) i dodatkowe części nagrań.

Nazwy walidatorów pól są w migracjach – zostają funkcjami na poziomie modułu.
"""
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError


def validate_allowed_url(value, hosts, message, *, allow_http=False):
    """Link tylko z podanych domen, bez loginu i hasła w adresie i z domyślnym portem; inaczej ``message``.

    Bez ``allow_http`` – tylko HTTPS (port 443)."""
    schemes, ports = (('http', 'https'), (None, 80, 443)) if allow_http else (('https',), (None, 443))
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in schemes and parsed.hostname in hosts
                 and not parsed.username and not parsed.password and parsed.port in ports)
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError(message)


def validate_youtube_url(value):
    validate_allowed_url(value, ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'),
                         'Wklej prawidłowy link do YouTube.', allow_http=True)


def validate_hearthis_url(value):
    validate_allowed_url(value, ('hearthis.at', 'www.hearthis.at'), 'Wklej prawidłowy link do HearThis.',
                         allow_http=True)


def validate_audio_links(value):
    """Additional parts use the same URL restrictions as primary links."""
    if not isinstance(value, list) or len(value) > 100:
        raise ValidationError('Podaj listę dodatkowych linków (maksymalnie 100).')
    seen = set()
    for link in value:
        if (not isinstance(link, dict) or set(link) != {'service', 'part', 'url'}
                or link['service'] not in ('youtube', 'hearthis')
                or type(link['part']) is not int or not 1 <= link['part'] <= 100
                or not isinstance(link['url'], str) or not 1 <= len(link['url']) <= 1000):
            raise ValidationError('Każdy link wymaga service (youtube/hearthis), part (1–100) i url.')
        (validate_youtube_url if link['service'] == 'youtube' else validate_hearthis_url)(link['url'])
        if link['url'] in seen:
            raise ValidationError('Dodatkowe linki nie mogą się powtarzać.')
        seen.add(link['url'])


def validate_mega_url(value):
    validate_allowed_url(value, ('mega.nz', 'www.mega.nz', 'mega.co.nz', 'www.mega.co.nz'),
                         'Wklej link HTTPS z mega.nz lub mega.co.nz.')
