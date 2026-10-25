"""Usuwanie folderów z listy CMS-u (DropboxFolder) razem z wierszami dostępu.

Najpierw sprawdzenie stanu w Dropboxie (check_folders, bez zmian), potem usunięcie tego samego stanu (skrót jak
w people.dropbox_import):
- folderu albo jego udostępnienia nie ma już w Dropboxie (GONE) albo folder nie jest udostępniony (UNSHARED) –
  nikt nie ma przez niego dostępu, więc folder i wiersze są usuwane bez wywołań API;
- folder istnieje i jest udostępniony (EXISTS) – administrator wybiera „przestań zarządzać” (członkowie zostają
  w Dropboxie) albo „odbierz dostęp, potem usuń” (zwykła synchronizacja; folder znika dopiero, gdy odbiór się udał);
- brak konfiguracji albo błąd API (UNKNOWN) – tylko „przestań zarządzać” po potwierdzeniu.
"""
import logging
from dataclasses import dataclass

from django.db import transaction

from people import dropbox_sharing
from people.dropbox_access import log_activity, revoke_rows, state_digest, sync
from people.dropbox_sharing import DropboxClient, DropboxError
from people.models import DropboxAccess, DropboxFolder

logger = logging.getLogger(__name__)

GONE, UNSHARED, EXISTS, UNKNOWN = "gone", "unshared", "exists", "unknown"
STATE_LABELS = {
    GONE: "Usunięty w Dropboxie", UNSHARED: "Nieudostępniony w Dropboxie", EXISTS: "Istnieje i jest udostępniony",
    UNKNOWN: "Stan nieznany",
}
STOP, REVOKE = "stop", "revoke"


@dataclass
class FolderCheck:
    folder: DropboxFolder
    state: str
    note: str = ""
    rows: int = 0  # wiersze dostępu (wszystkie, także odebrane)
    granted: int = 0  # wiersze z adresem, który ma dostęp nadany albo przejęty przez CMS

    @property
    def label(self):
        return STATE_LABELS[self.state]

    @property
    def needs_choice(self):
        return self.state in (EXISTS, UNKNOWN)

    def signature(self):
        return [self.folder.pk, self.folder.path, self.folder.shared_folder_id, self.state, self.rows, self.granted]


def check_folders(folders, client=None):
    """Stan folderów w Dropboxie (tylko odczyty). Brak konfiguracji i błędy API – UNKNOWN z opisem."""
    error = ""
    if client is None:
        if not dropbox_sharing.is_configured():
            error = dropbox_sharing.NOT_CONFIGURED
        else:
            client = DropboxClient()
    checks = []
    for folder in folders:
        rows = DropboxAccess.objects.filter(folder=folder)
        check = FolderCheck(folder, UNKNOWN, error, rows.count(), rows.exclude(granted_email="").count())
        if not error:
            try:
                check.state, check.note = _state(client, folder)
            except DropboxError as exception:
                check.note = str(exception)
        checks.append(check)
    return checks


def _state(client, folder):
    """(stan, opis). Z identyfikatorem udostępnienia sprawdzany jest sam udostępniony folder – po przeniesieniu
    albo zmianie nazwy ścieżka w CMS-ie nie istnieje, a członkowie wciąż mają dostęp."""
    if folder.shared_folder_id:
        try:
            metadata = client.shared_folder_metadata(folder.shared_folder_id)
        except DropboxError as error:
            if dropbox_sharing.is_gone(error):
                return GONE, str(error)
            raise
        path = metadata.get("path_lower") or ""
        if not path:
            return GONE, "Udostępniony folder nie jest już w Dropboxie konta aplikacji (usunięty)."
        if path != folder.path.casefold():
            return EXISTS, f"Folder jest teraz w Dropboxie pod ścieżką {metadata.get('path_display') or path}."
        return EXISTS, ""
    try:
        metadata = client.folder_metadata(folder.path)
    except DropboxError as error:
        if dropbox_sharing.is_gone(error):
            return GONE, str(error)
        raise
    if dropbox_sharing._shared_id(metadata):
        return EXISTS, "Folder udostępniono w Dropboxie poza CMS-em."
    return UNSHARED, "Folder nie jest udostępniony – nikt nie ma przez niego dostępu."


def checks_digest(checks):
    return state_digest([check.signature() for check in checks])


def remove(check, mode, *, actor=None):
    """Usuwa folder według stanu i wyboru; zwraca (usunięty, wyniki synchronizacji z błędami)."""
    if check.needs_choice and mode not in ((STOP, REVOKE) if check.state == EXISTS else (STOP,)):
        return False, []
    if check.state == EXISTS and mode == REVOKE:
        rows = DropboxAccess.objects.filter(folder=check.folder)
        results = sync(revoke_rows(rows), actor=actor)  # bez sygnałów – synchronizacja od razu
        failed = [result for result in results if result.error]
        if failed or rows.exclude(granted_email="").exists():
            return False, failed
    how = {GONE: "usunięty w Dropboxie", UNSHARED: "nieudostępniony", EXISTS: "dostęp odebrany"}.get(check.state, "")
    if mode == STOP:
        how = "członkowie zostali w Dropboxie"
    with transaction.atomic():
        DropboxAccess.objects.filter(folder=check.folder).delete()
        DropboxFolder.objects.filter(pk=check.folder.pk).delete()
        transaction.on_commit(lambda: log_activity(
            actor, f"Dropbox: usunięcie folderu z listy ({how})", check.folder.path, "/dropbox/remove_folder",
            "Nie udało się zapisać usunięcia folderu Dropbox w dzienniku aktywności.", logger))
    return True, []


def missing_in_listing(listing, root):
    """Foldery CMS-u bezpośrednio w folderze głównym, których nie ma na liście podfolderów z Dropboxa:
    [(folder, ścieżka w Dropboxie po przeniesieniu albo pusta)]."""
    paths = {entry["path"].casefold() for entry in listing}
    by_share = {entry["shared_folder_id"]: entry["path"] for entry in listing if entry.get("shared_folder_id")}
    prefix = root.rstrip("/").casefold() + "/"
    missing = []
    for folder in DropboxFolder.objects.order_by("name", "pk"):
        path = folder.path.casefold()
        if path.startswith(prefix) and "/" not in path[len(prefix):] and path not in paths:
            missing.append((folder, by_share.get(folder.shared_folder_id, "") if folder.shared_folder_id else ""))
    return missing
