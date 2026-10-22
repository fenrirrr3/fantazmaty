"""Klient API Dropboxa (v2) do udostępniania folderów: odświeżanie tokenu, foldery i członkowie folderów.

Bez SDK Dropboxa – wystarcza urllib ze standardowej biblioteki (projekt nie ma też requests). Konto, dla którego
wygenerowano refresh token, jest właścicielem folderów. Każdy błąd API kończy się DropboxError z komunikatem
po polsku, który trafia do panelu admina i do wiersza dostępu.
"""
import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

API_URL = "https://api.dropboxapi.com/2/"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
TIMEOUT = 20
# Zadania asynchroniczne (udostępnienie folderu, usunięcie członka) sprawdzane co JOB_DELAY s, najwyżej JOB_ATTEMPTS razy.
JOB_ATTEMPTS = 15
JOB_DELAY = 1.0
# Token dostępu ważny jest ok. 4 godzin; odświeżany z zapasem.
TOKEN_MARGIN = 300

NOT_CONFIGURED = ("Dropbox nie jest skonfigurowany – uzupełnij DROPBOX_APP_KEY, DROPBOX_APP_SECRET "
                  "i DROPBOX_REFRESH_TOKEN w pliku .env.")

# Fragmenty error_summary Dropboxa (np. „add_folder_member/bad_member/invalid_email/..”) → komunikat.
# Kolejność ma znaczenie: pierwsze dopasowanie wygrywa, więc szczegółowe przyczyny są przed ogólnymi.
ERROR_MESSAGES = (
    ("invalid_email", "Dropbox uznał adres e-mail za niepoprawny."),
    ("bad_member", "Dropbox nie przyjął adresu tej osoby."),
    ("email_unverified", "Konto Dropbox właściciela folderów ma niepotwierdzony adres e-mail – potwierdź go w Dropboxie."),
    ("banned_member", "Ta osoba jest zablokowana w tym folderze przez właściciela."),
    ("cant_share_outside_team", "Zasady zespołu Dropbox nie pozwalają udostępniać folderów poza zespół."),
    ("too_many_members", "Folder osiągnął limit członków Dropboxa."),
    ("too_many_pending_invites", "Folder ma za dużo oczekujących zaproszeń – poczekaj, aż osoby je przyjmą."),
    ("too_many_invitees", "Za dużo zaproszeń naraz – spróbuj ponownie później."),
    ("rate_limit", "Przekroczono limit zaproszeń Dropboxa – spróbuj ponownie później."),
    ("insufficient_plan", "Plan konta Dropbox nie pozwala na tę operację."),
    ("inside_shared_folder", "Folder leży wewnątrz innego udostępnionego folderu – Dropbox nie pozwala udostępnić go osobno."),
    ("contains_shared_folder", "Folder zawiera udostępnione podfoldery – Dropbox nie pozwala udostępnić go w całości."),
    ("is_file", "Ścieżka wskazuje plik, a nie folder."),
    ("not_found", "Na koncie Dropbox aplikacji nie ma takiego folderu."),
    ("member_error/not_a_member", "Tej osoby nie ma już w folderze w Dropboxie – synchronizuj ponownie."),
    ("no_explicit_access","Ta osoba ma dostęp przez folder nadrzędny – zmień go w Dropboxie przy tamtym folderze."),
    ("team_folder", "To folder zespołu Dropbox – jego członków nie można zmieniać przez API."),
    ("invalid_id", "Dropbox nie zna tego udostępnionego folderu – sprawdź ścieżkę folderu w CMS-ie."),
    ("no_permission", "Konto aplikacji nie ma uprawnień do zarządzania tym folderem (folder nie należy do konta)."),
    ("not_a_member", "Konto aplikacji nie jest członkiem tego folderu (folder nie należy do konta)."),
    ("access_error", "Konto aplikacji nie ma dostępu do tego folderu."),
)


class DropboxError(Exception):
    """Błąd Dropboxa z komunikatem dla człowieka; summary to surowy error_summary z API (do logów i dopasowań)."""

    def __init__(self, message, summary=""):
        super().__init__(message)
        self.summary = summary


class DropboxNotConfigured(DropboxError):
    pass


@dataclass(frozen=True)
class Member:
    """Członek udostępnionego folderu: poziom dostępu Dropboxa i rodzaj (konto albo zaproszenie bez konta)."""
    level: str
    invitee: bool = False
    inherited: bool = False


def is_configured():
    return all(getattr(settings, name, "") for name in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"))


def message_for(summary):
    for fragment, message in ERROR_MESSAGES:
        if fragment in summary:
            return message
    return f"Dropbox zwrócił błąd: {summary or 'bez opisu'}."


def _send(url, data, headers):
    """Jedno zapytanie POST; zwraca (kod HTTP, treść, nagłówki). Błędy połączenia – DropboxError."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(), error.headers
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise DropboxError(f"Brak połączenia z Dropboxem ({reason}). Spróbuj ponownie później.") from None


def _json(body):
    try:
        return json.loads(body.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        return None


def _http_error(status, body, headers):
    """Komunikat dla odpowiedzi innej niż 200 (poza 409, który ma opis błędu w JSON-ie)."""
    data = _json(body)
    if status == 401:
        error = (data or {}).get("error") or {}
        if error.get(".tag") == "missing_scope":
            scope = error.get("required_scope", "")
            return DropboxError(
                f"Aplikacji Dropbox brakuje uprawnienia „{scope}” – zaznacz je w konsoli aplikacji (zakładka "
                "Permissions) i wygeneruj nowy refresh token.", f"missing_scope/{scope}")
        return DropboxError("Dropbox odrzucił token dostępu – wygeneruj nowy refresh token (README).",
                            (data or {}).get("error_summary", "invalid_access_token"))
    if status == 429:
        wait = (headers or {}).get("Retry-After")
        when = f"za {wait} s" if wait else "za chwilę"
        return DropboxError(f"Dropbox ogranicza liczbę zapytań – spróbuj ponownie {when}.", "too_many_requests")
    if status >= 500:
        return DropboxError(f"Dropbox jest chwilowo niedostępny (HTTP {status}) – spróbuj ponownie później.",
                            f"http_{status}")
    text = body.decode("utf-8", "replace").strip()[:300]
    return DropboxError(f"Dropbox odrzucił zapytanie (HTTP {status}): {text or 'bez opisu'}", f"http_{status}")


class DropboxClient:
    """Wywołania API v2 potrzebne do udostępniania folderów; token dostępu w pamięci podręcznej do wygaśnięcia."""

    def __init__(self):
        if not is_configured():
            raise DropboxNotConfigured(NOT_CONFIGURED, "not_configured")
        self.app_key = settings.DROPBOX_APP_KEY
        self.app_secret = settings.DROPBOX_APP_SECRET
        self.refresh_token = settings.DROPBOX_REFRESH_TOKEN
        digest = hashlib.sha256(f"{self.app_key}:{self.refresh_token}".encode()).hexdigest()[:24]
        self.cache_key = f"dropbox-access-token:{digest}"

    # Token i wywołania

    def access_token(self, *, refresh=False):
        token = None if refresh else cache.get(self.cache_key)
        if token:
            return token
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
            "client_id": self.app_key, "client_secret": self.app_secret,
        }).encode()
        status, body, headers = _send(TOKEN_URL, data, {"Content-Type": "application/x-www-form-urlencoded"})
        payload = _json(body) or {}
        if status != 200 or not payload.get("access_token"):
            error = payload.get("error", "")
            if error == "invalid_grant":
                raise DropboxError("Refresh token Dropboxa jest nieważny lub odwołany – wygeneruj nowy (README).", error)
            if error in ("invalid_client", "unauthorized_client"):
                raise DropboxError("Klucz lub sekret aplikacji Dropbox jest niepoprawny – sprawdź DROPBOX_APP_KEY "
                                   "i DROPBOX_APP_SECRET.", error)
            raise _http_error(status if status != 200 else 502, body, headers)
        token = payload["access_token"]
        lifetime = int(payload.get("expires_in") or 14400)
        cache.set(self.cache_key, token, max(lifetime - TOKEN_MARGIN, 60))
        return token

    def call(self, route, arguments):
        """Wywołanie RPC; przy wygasłym tokenie jedno ponowienie z nowym. Błąd 409 → DropboxError z opisem."""
        body = json.dumps(arguments).encode()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.access_token(refresh=attempt > 0)}",
                       "Content-Type": "application/json"}
            status, response, response_headers = _send(API_URL + route, body, headers)
            if status == 401 and attempt == 0:
                tag = ((_json(response) or {}).get("error") or {}).get(".tag")
                if tag in ("expired_access_token", "invalid_access_token"):
                    cache.delete(self.cache_key)
                    continue
            break
        if status == 200:
            data = _json(response)
            # „null” to poprawna odpowiedź tras bez wyniku (add_folder_member); inna nieczytelna treść – błąd.
            if data is None and response.strip() not in (b"", b"null"):
                raise DropboxError("Dropbox zwrócił nieczytelną odpowiedź – spróbuj ponownie później.", "invalid_json")
            return data
        if status == 409:
            data = _json(response) or {}
            summary = data.get("error_summary", "")
            error = DropboxError(message_for(summary), summary)
            error.data = data.get("error")
            raise error
        raise _http_error(status, response, response_headers)

    def _wait(self, route, job_id):
        """Czeka na zadanie asynchroniczne; zwraca wynik „complete” albo zgłasza błąd z „failed”."""
        for _ in range(JOB_ATTEMPTS):
            result = self.call(route, {"async_job_id": job_id})
            tag = result.get(".tag")
            if tag == "complete":
                return result
            if tag == "failed":
                summary = _summary(result.get("failed"))
                raise DropboxError(message_for(summary), summary)
            time.sleep(JOB_DELAY)
        raise DropboxError("Dropbox wciąż przetwarza zmianę – sprawdź stan za kilka minut (synchronizuj ponownie).",
                           "job_in_progress")

    # Foldery

    def folder_metadata(self, path):
        metadata = self.call("files/get_metadata", {"path": path})
        if metadata.get(".tag") != "folder":
            raise DropboxError(message_for("is_file"), "is_file")
        return metadata

    def list_subfolders(self, path):
        """Podfoldery (bez zagłębiania): nazwa, ścieżka i identyfikator udostępnienia (pusty – nieudostępniony)."""
        result = self.call("files/list_folder", {"path": path, "recursive": False, "include_mounted_folders": True})
        entries = list(result.get("entries", []))
        while result.get("has_more"):
            result = self.call("files/list_folder/continue", {"cursor": result["cursor"]})
            entries.extend(result.get("entries", []))
        folders = [{"name": entry["name"], "path": entry.get("path_display") or entry["path_lower"],
                    "shared_folder_id": _shared_id(entry)}
                   for entry in entries if entry.get(".tag") == "folder"]
        return sorted(folders, key=lambda folder: folder["name"].casefold())

    def ensure_shared(self, path):
        """Identyfikator udostępnienia folderu; nieudostępniony folder zostaje udostępniony (konto aplikacji – właściciel)."""
        metadata = self.folder_metadata(path)
        shared = _shared_id(metadata)
        if shared:
            return shared
        if (metadata.get("sharing_info") or {}).get("parent_shared_folder_id"):
            raise DropboxError(message_for("inside_shared_folder"), "inside_shared_folder")
        try:
            result = self.call("sharing/share_folder", {"path": path, "force_async": False})
        except DropboxError as error:
            # Folder udostępniony w międzyczasie: błąd zawiera jego metadane. Dropbox wpisuje pola struktury
            # (SharedFolderMetadata) obok „.tag”: {"bad_path": {".tag": "already_shared", "shared_folder_id": …}}.
            bad_path = (getattr(error, "data", None) or {}).get("bad_path") or {}
            if bad_path.get(".tag") == "already_shared":
                already = bad_path.get("already_shared") or bad_path
                if already.get("shared_folder_id"):
                    return already["shared_folder_id"]
            raise
        if result.get(".tag") == "async_job_id":
            result = self._wait("sharing/check_share_job_status", result["async_job_id"])
        if not result.get("shared_folder_id"):
            raise DropboxError("Dropbox nie zwrócił identyfikatora udostępnionego folderu.", "missing_shared_folder_id")
        return result["shared_folder_id"]

    # Członkowie

    def folder_members(self, shared_folder_id):
        """{adres e-mail małymi literami: Member} – konta i zaproszenia bez konta (invitees)."""
        result = self.call("sharing/list_folder_members", {"shared_folder_id": shared_folder_id, "limit": 200})
        members = {}
        while True:
            for entry in result.get("users", []):
                email = (entry.get("user") or {}).get("email", "")
                if email:
                    members[email.lower()] = Member(entry["access_type"][".tag"], False, bool(entry.get("is_inherited")))
            for entry in result.get("invitees", []):
                invitee = entry.get("invitee") or {}
                if invitee.get(".tag") == "email":
                    members[invitee["email"].lower()] = Member(entry["access_type"][".tag"], True,
                                                               bool(entry.get("is_inherited")))
            if not result.get("cursor"):
                return members
            result = self.call("sharing/list_folder_members/continue", {"cursor": result["cursor"]})

    def add_member(self, shared_folder_id, email, level):
        """Dodaje osobę; bez konta Dropbox dostaje zaproszenie e-mailem (quiet=False – Dropbox wysyła powiadomienie)."""
        self.call("sharing/add_folder_member", {
            "shared_folder_id": shared_folder_id, "quiet": False,
            "members": [{"member": {".tag": "email", "email": email}, "access_level": {".tag": level}}],
        })

    def update_member(self, shared_folder_id, email, level):
        self.call("sharing/update_folder_member", {
            "shared_folder_id": shared_folder_id, "member": {".tag": "email", "email": email},
            "access_level": {".tag": level},
        })

    def remove_member(self, shared_folder_id, email):
        """Usuwa osobę bez zostawiania jej kopii plików; osoba, której już nie ma w folderze, nie jest błędem."""
        try:
            result = self.call("sharing/remove_folder_member", {
                "shared_folder_id": shared_folder_id, "member": {".tag": "email", "email": email},
                "leave_a_copy": False,
            })
            if result.get(".tag") == "async_job_id":
                self._wait("sharing/check_remove_member_job_status", result["async_job_id"])
        except DropboxError as error:
            if "member_error/not_a_member" not in error.summary:
                raise


def _shared_id(metadata):
    return (metadata.get("sharing_info") or {}).get("shared_folder_id") or metadata.get("shared_folder_id") or ""


def _summary(error):
    """error_summary z obiektu błędu zadania asynchronicznego (np. {".tag": "member_error", "member_error": {...}})."""
    parts = []
    while isinstance(error, dict) and error.get(".tag"):
        parts.append(error[".tag"])
        error = error.get(error[".tag"])
    return "/".join(parts)
