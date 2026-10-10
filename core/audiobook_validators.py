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
