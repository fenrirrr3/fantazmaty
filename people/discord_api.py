"""Klient REST API Discorda (v10) dla bota: członkowie serwera, rangi serwera, nadanie i odebranie rangi.

Bez stałego połączenia (gateway) – wystarczają pojedyncze zapytania HTTPS (PythonAnywhere). Bez bibliotek
zewnętrznych – urllib ze standardowej biblioteki. Każdy błąd kończy się DiscordError z komunikatem po polsku,
który trafia do panelu admina i do historii synchronizacji rang.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

logger = logging.getLogger(__name__)

API_URL = "https://discord.com/api/v10"
TIMEOUT = 15
USER_AGENT = "DiscordBot (https://cms.fantazmaty.pl, 1.0) FantazmatyCMS"
PAGE_SIZE = 1000
# Limit zapytań (429): ponowienie po retry_after, najwyżej RATE_LIMIT_ATTEMPTS razy i gdy czekanie nie przekracza
# max_wait sekund (w żądaniu z panelu krótko, w komendzie okresowej dłużej).
RATE_LIMIT_ATTEMPTS = 3
DEFAULT_MAX_WAIT = 10.0
# Discord przyjmuje powód (X-Audit-Log-Reason) do 512 znaków po zakodowaniu URL.
REASON_LIMIT = 512

NOT_CONFIGURED = ("Bot Discorda nie jest skonfigurowany – uzupełnij DISCORD_BOT_TOKEN i DISCORD_GUILD_ID w pliku "
                  ".env i przeładuj aplikację. Do tego czasu CMS nie nadaje ani nie odbiera rang.")
MEMBERS_INTENT = ("Discord odmówił pobrania listy członków serwera (HTTP 403). Włącz „Server Members Intent” "
                  "w ustawieniach bota (Discord Developer Portal → aplikacja → Bot → Privileged Gateway Intents) "
                  "i sprawdź, czy bot jest na serwerze o identyfikatorze z DISCORD_GUILD_ID.")
HIERARCHY = ("Discord odmówił zmiany rangi (HTTP 403). Bot musi mieć uprawnienie „Zarządzanie rolami”, a jego "
             "ranga musi być na liście rang serwera wyżej niż ranga, którą nadaje lub odbiera (Ustawienia serwera → "
             "Role – przeciągnij rangę bota nad rangi zarządzane przez CMS).")
UNAUTHORIZED = "Discord odrzucił token bota (HTTP 401) – sprawdź DISCORD_BOT_TOKEN (Developer Portal → Bot → Reset Token)."
NOT_MEMBER = "Tej osoby nie ma na serwerze Discorda (albo identyfikator jest błędny)."
UNKNOWN_ROLE = "Na serwerze Discorda nie ma rangi o tym identyfikatorze – popraw ID roli w „Role Discorda”."
UNKNOWN_GUILD = "Discord nie zna serwera z DISCORD_GUILD_ID albo bota na nim nie ma – sprawdź identyfikator i zaproś bota."

# Kody błędów JSON Discorda (pole „code”), dla których komunikat zależy od przyczyny, a nie tylko od statusu HTTP.
UNKNOWN_GUILD_CODE = 10004
UNKNOWN_ROLE_CODE = 10011
UNKNOWN_MEMBER_CODES = (10007, 10013)


class DiscordError(Exception):
    """Błąd Discorda z komunikatem dla człowieka; status i code – z odpowiedzi (do dopasowań i historii)."""

    def __init__(self, message, status=0, code=0, detail=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail

    @property
    def not_member(self):
        return self.status == 404 and self.code in UNKNOWN_MEMBER_CODES

    def short(self):
        """Odpowiedź Discorda w skrócie (do historii)."""
        parts = [f"HTTP {self.status}" if self.status else "brak odpowiedzi"]
        if self.code:
            parts.append(f"kod {self.code}")
        if self.detail:
            parts.append(self.detail)
        return ", ".join(parts)[:300]


class DiscordNotConfigured(DiscordError):
    pass


def is_configured():
    return bool(settings.DISCORD_BOT_TOKEN and settings.DISCORD_GUILD_ID)


@sensitive_variables("token")
def _send(method, url, token, headers):
    """Jedno zapytanie; zwraca (kod HTTP, treść, nagłówki). Błędy połączenia – DiscordError."""
    request = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bot {token}", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(), error.headers
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise DiscordError(f"Brak połączenia z Discordem ({reason}). Spróbuj ponownie później.") from None


def _json(body):
    try:
        return json.loads((body or b"").decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        return None


def encode_reason(reason):
    """Powód do dziennika zmian serwera: zakodowany URL, przycięty do limitu Discorda."""
    text = " ".join((reason or "").split())
    encoded = urllib.parse.quote(text, safe="")
    while len(encoded) > REASON_LIMIT:
        text = text[:-10]
        encoded = urllib.parse.quote(text + "…", safe="")
    return encoded


class DiscordClient:
    """Wywołania API potrzebne do synchronizacji rang; sleep i max_wait do obsługi limitu zapytań (429)."""

    def __init__(self, *, max_wait=DEFAULT_MAX_WAIT, sleep=None):
        if not is_configured():
            raise DiscordNotConfigured(NOT_CONFIGURED)
        self.token = settings.DISCORD_BOT_TOKEN
        self.guild_id = str(settings.DISCORD_GUILD_ID)
        self.max_wait = max_wait
        self.sleep = sleep or time.sleep

    def request(self, method, path, *, query=None, reason="", context=""):
        """Zapytanie z obsługą 429 (ponowienie po retry_after); zwraca (status, dane JSON). Błąd – DiscordError.

        context („members” albo „role”) wybiera komunikat dla 403 i 404."""
        url = API_URL + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        headers = {"X-Audit-Log-Reason": encode_reason(reason)} if reason else {}
        for attempt in range(RATE_LIMIT_ATTEMPTS + 1):
            status, body, response_headers = _send(method, url, self.token, headers)
            data = _json(body)
            if status != 429:
                break
            wait = _retry_after(data, response_headers)
            if attempt == RATE_LIMIT_ATTEMPTS or wait > self.max_wait:
                raise DiscordError(f"Discord ogranicza liczbę zapytań – spróbuj ponownie za {wait:.0f} s.", 429,
                                   detail=f"retry_after {wait:g} s")
            logger.info("Limit zapytań Discorda – ponowienie za %s s.", wait)
            self.sleep(wait)
        if 200 <= status < 300:
            return status, data
        raise _error(status, data, body, context)

    def members(self):
        """Wszyscy członkowie serwera (stronicowanie po identyfikatorze, po PAGE_SIZE na stronę)."""
        result, after = [], "0"
        while True:
            _, page = self.request("GET", f"/guilds/{self.guild_id}/members",
                                   query={"limit": PAGE_SIZE, "after": after}, context="members")
            if not isinstance(page, list):
                raise DiscordError("Discord zwrócił nieczytelną listę członków serwera.", 200)
            result.extend(page)
            if len(page) < PAGE_SIZE:
                return result
            after = page[-1]["user"]["id"]

    def member(self, user_id):
        """Członek serwera albo None, gdy osoby nie ma na serwerze."""
        try:
            return self.request("GET", f"/guilds/{self.guild_id}/members/{user_id}", context="member")[1]
        except DiscordError as error:
            if error.not_member:
                return None
            raise

    def roles(self):
        """Rangi serwera: {id: {name, position, managed}}."""
        _, data = self.request("GET", f"/guilds/{self.guild_id}/roles", context="members")
        return {str(role["id"]): role for role in data or []}

    def add_role(self, user_id, role_id, reason):
        self.request("PUT", f"/guilds/{self.guild_id}/members/{user_id}/roles/{role_id}", reason=reason, context="role")

    def remove_role(self, user_id, role_id, reason):
        self.request("DELETE", f"/guilds/{self.guild_id}/members/{user_id}/roles/{role_id}", reason=reason,
                     context="role")


def _retry_after(data, headers):
    value = (data or {}).get("retry_after") if isinstance(data, dict) else None
    if value is None:
        value = (headers or {}).get("Retry-After")
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 1.0


def _error(status, data, body, context):
    data = data if isinstance(data, dict) else {}
    code = data.get("code") or 0
    detail = str(data.get("message") or (body or b"").decode("utf-8", "replace").strip())[:200]
    if status == 401:
        message = UNAUTHORIZED
    elif status == 403:
        message = HIERARCHY if context == "role" else MEMBERS_INTENT
    elif status == 404 and code in UNKNOWN_MEMBER_CODES:
        message = NOT_MEMBER
    elif status == 404 and code == UNKNOWN_ROLE_CODE:
        message = UNKNOWN_ROLE
    elif status == 404 and code == UNKNOWN_GUILD_CODE:
        message = UNKNOWN_GUILD
    elif status >= 500:
        message = f"Discord jest chwilowo niedostępny (HTTP {status}) – spróbuj ponownie później."
    else:
        message = f"Discord odrzucił zapytanie (HTTP {status}): {detail or 'bez opisu'}"
    return DiscordError(message, status, code, detail)


def display_name(member):
    """Nazwa widoczna na serwerze: pseudonim serwerowy, potem nazwa globalna, potem nazwa użytkownika."""
    user = member.get("user") or {}
    return member.get("nick") or user.get("global_name") or user.get("username") or ""
