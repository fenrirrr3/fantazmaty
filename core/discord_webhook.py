"""Outbound Discord transport. Only configured HTTPS Discord webhook endpoints."""
import json
import os
import re
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

WEBHOOK = re.compile(r'https://discord\.com/api/(?:v10/)?webhooks/[0-9]+/[A-Za-z0-9._-]+')

class ConfigurationError(Exception):
    pass

class DeliveryError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

@sensitive_variables()
def channels():
    raw = getattr(settings, 'DISCORD_WEBHOOKS', None)
    if raw is None:
        try:
            raw = json.loads(os.environ.get('DISCORD_WEBHOOKS', '{}'))
        except (ValueError, TypeError):
            raise ConfigurationError('Nieprawidłowa konfiguracja kanałów Discorda.') from None
    if not isinstance(raw, dict) or len(raw) > 20:
        raise ConfigurationError('Konfiguracja wymaga słownika: nazwa kanału → webhook (maks. 20).')
    result = {}
    for name, url in raw.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or not isinstance(url, str) or not WEBHOOK.fullmatch(url):
            raise ConfigurationError('Sprawdź nazwy kanałów i adresy webhooków w konfiguracji serwera.')
        result[name] = url
    return result

@sensitive_variables()
def send_message(url, content):
    if not WEBHOOK.fullmatch(url):
        raise DeliveryError('Nieprawidłowy adres webhooka.')
    payload = json.dumps({'content': content, 'allowed_mentions': {'parse': []}}, ensure_ascii=False).encode('utf-8')
    request = Request(url + '?wait=true', data=payload, method='POST', headers={
        'Content-Type': 'application/json', 'User-Agent': 'FantazmatyCMS-DiscordTest/1.0',
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise DeliveryError('Discord nie potwierdził wysłania. Sprawdź kanał przed ponowną próbą.', uncertain=True)
            data = json.loads(response.read(65536).decode('utf-8'))
            message_id = data.get('id') if isinstance(data, dict) else None
            if not isinstance(message_id, str) or not message_id.isascii() or not message_id.isdigit() or len(message_id) > 30:
                raise DeliveryError('Nie otrzymano identyfikatora wiadomości. Sprawdź kanał przed ponowną próbą.', uncertain=True)
            return message_id
    except HTTPError as exc:
        if exc.code == 429:
            raise DeliveryError('Discord ograniczył liczbę wysyłek. Odczekaj chwilę i spróbuj ponownie.') from None
        if exc.code in (401,403,404):
            raise DeliveryError('Webhook jest niedostępny. Sprawdź jego konfigurację i uprawnienia na Discordzie.') from None
        raise DeliveryError('Discord odrzucił żądanie.' if exc.code < 500 else 'Błąd Discorda. Sprawdź kanał przed ponowną próbą.', uncertain=exc.code >= 500) from None
    except (URLError, OSError, ValueError) as exc:
        raise DeliveryError('Brak potwierdzenia wysyłki. Sprawdź kanał przed ponowną próbą — wiadomość mogła dotrzeć.', uncertain=True) from None
