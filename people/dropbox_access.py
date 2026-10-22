"""Nadawanie i odbieranie dostępu do folderów Dropbox członkom zespołu (wiersze DropboxAccess).

Zapis w CMS-ie (osoba, jej adres Dropbox, „wciąż w ekipie”, poziom dostępu, usunięcie osoby) tylko oznacza wiersze
do synchronizacji. Połączenie z Dropboxem następuje po zatwierdzeniu transakcji (transaction.on_commit), więc
wycofany zapis niczego w Dropboxie nie zmienia, a błąd Dropboxa nie cofa zapisu osoby – trafia do wiersza dostępu
i do komunikatu w adminie. Ponowną próbę uruchamia akcja „Synchronizuj ponownie” albo komenda sync_dropbox_access.

CMS zarządza wyłącznie folderami z listy DropboxFolder i usuwa z nich wyłącznie adresy, którym sam nadał dostęp
(granted_email); osób dodanych w Dropboxie ręcznie nie rusza, dopóki ich dostęp nie zostanie przejęty
(people.dropbox_import) – od tej chwili zarządza nim jak nadanym.
"""
import logging
from dataclasses import dataclass

from django.db import connections, transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from people import dropbox_sharing
from people.dropbox_sharing import DropboxClient, DropboxError, Member
from people.models import DropboxAccess, DropboxFolder, Person

logger = logging.getLogger(__name__)

State = DropboxAccess.State
LEVEL_LABELS = dict(DropboxAccess.Level.choices)
ACTION_LABELS = {
    "grant": "nadanie dostępu", "invite": "zaproszenie do folderu", "update": "zmiana poziomu dostępu",
    "remove": "odebranie dostępu", "import": "przejęcie dostępu z Dropboxa",
}
FOLDER_GONE = ("Folderu nie ma już w Dropboxie – usuń go z listy (Foldery Dropbox → akcja „Usuń z listy”) "
               "albo popraw ścieżkę.")
NOT_CONFIGURED_NOTICE = ("Dropbox nie jest skonfigurowany – dostęp zostanie nadany po konfiguracji "
                         "(komenda sync_dropbox_access).")


def person_is_member(person):
    """Członek zespołu w rozumieniu dostępu do folderów: „wciąż w ekipie” i nie zewnętrzny."""
    return bool(person and person.is_active and not person.is_external)


def target_email(access):
    """Adres, który powinien mieć dostęp – pusty, gdy dostęp ma zostać odebrany albo nie ma go komu nadać."""
    person = access.person
    if not access.wanted or not person_is_member(person):
        return ""
    return (person.dropbox_email or "").strip().lower()


def waiting_state(access):
    """Stan wiersza przed synchronizacją."""
    if target_email(access):
        return State.PENDING
    if access.granted_email:
        return State.TO_REMOVE
    return State.PENDING if access.wanted and person_is_member(access.person) else State.REMOVED


@dataclass
class Result:
    access: DropboxAccess
    action: str = ""  # grant, invite, update, remove albo pusty (bez zmian)
    error: str = ""
    skipped: bool = False  # Dropbox nieskonfigurowany
    removed: str = ""  # adres usunięty z folderu (odebranie albo zmiana adresu)

    def describe(self):
        access = self.access
        who = str(access.person) if access.person_id else f"osoba usunięta ({access.granted_email or 'bez adresu'})"
        where = f"{who} → {access.folder.name}"
        if self.error:
            return f"Dropbox – {where}: {self.error}"
        if self.action in ("grant", "invite", "update"):
            detail = f" ({LEVEL_LABELS[access.level]})"
            if self.action == "invite":
                detail += " – Dropbox wysłał zaproszenie, osoba nie ma jeszcze konta"
            if self.removed:
                detail += f"; usunięto poprzedni adres {self.removed}"
            return f"Dropbox – {where}: {ACTION_LABELS[self.action]} dla {access.granted_email}{detail}."
        if self.action == "remove":
            return f"Dropbox – {where}: odebrano dostęp ({self.removed})."
        return f"Dropbox – {where}: bez zmian."


# Planowanie po zatwierdzeniu transakcji

# Partia z jednej transakcji żyje na obiekcie połączenia (jak partie powiadomień Discord) i znika wraz z jej końcem.
_PENDING_ATTR = "_dropbox_access_batch"


class _Batch:
    def __init__(self, using):
        self.using = using
        self.ids = set()
        self.actor = None
        self.notifiers = []
        self.registered = False

    def register(self):
        # Poza transakcją on_commit wykonuje partię od razu – dlatego wiersze dopisuje się przed rejestracją.
        if not self.registered:
            self.registered = True
            transaction.on_commit(self.flush, using=self.using)

    def is_registered(self, connection):
        return any(entry[1] == self.flush for entry in connection.run_on_commit)

    def flush(self):
        connection = connections[self.using]
        if getattr(connection, _PENDING_ATTR, None) is self:
            delattr(connection, _PENDING_ATTR)
        if not self.ids:
            return
        try:
            results = sync(DropboxAccess.objects.using(self.using).filter(pk__in=self.ids), actor=self.actor)
        except Exception:
            # Błąd Dropboxa nie może zamienić zapisanej już zmiany w błąd 500.
            logger.exception("Synchronizacja dostępu do Dropboxa nie powiodła się.")
            results = []
        for notify in self.notifiers:
            notify(results)


def _batch(using, create):
    connection = connections[using]
    batch = getattr(connection, _PENDING_ATTR, None)
    if batch is not None and batch.is_registered(connection):
        return batch
    if not create:
        return None
    batch = _Batch(using)
    setattr(connection, _PENDING_ATTR, batch)
    return batch


def schedule(ids, using="default"):
    """Oznacza wiersze jako czekające i synchronizuje je po zatwierdzeniu transakcji (poza transakcją – od razu)."""
    ids = {pk for pk in ids if pk}
    if not ids:
        return
    for access in DropboxAccess.objects.using(using).select_related("person").filter(pk__in=ids):
        state = waiting_state(access)
        if state != access.state:
            DropboxAccess.objects.using(using).filter(pk=access.pk).update(state=state)
    batch = _batch(using, create=True)
    batch.ids |= ids
    batch.register()


def report_to(request, notify, using="default"):
    """Wyniki synchronizacji z bieżącej transakcji trafią do notify(results); działania zapisze się na konto z request.

    Wywołuj w transakcji, przed zapisem (np. w atomic akcji admina), żeby partia nie wykonała się wcześniej."""
    batch = _batch(using, create=True)
    batch.actor = getattr(request, "user", None)
    batch.notifiers.append(notify)
    batch.register()


def admin_messages(model_admin, request):
    """notify dla report_to: komunikaty panelu admina (sukcesy, błędy, brak konfiguracji)."""
    from django.contrib import messages

    def notify(results):
        if any(result.skipped for result in results):
            model_admin.message_user(request, NOT_CONFIGURED_NOTICE, messages.WARNING)
        for result in results:
            if result.error and not result.skipped:
                model_admin.message_user(request, result.describe(), messages.ERROR)
            elif result.action:
                model_admin.message_user(request, result.describe(), messages.SUCCESS)
    return notify


# Synchronizacja

def sync(accesses, *, actor=None, client=None, dry_run=False):
    """Uzgadnia wiersze z Dropboxem; zwraca listę Result. dry_run – tylko odczyty z Dropboxa, bez zmian i zapisów."""
    accesses = list(accesses.select_related("person", "folder").order_by("folder_id", "pk"))
    if not accesses:
        return []
    if client is None:
        if dropbox_sharing.is_configured():
            client = DropboxClient()
        elif not dry_run:
            message = dropbox_sharing.NOT_CONFIGURED
            return [Result(access, error=message, skipped=True) for access in accesses]
    if dry_run:
        client = _DryRun(client)
    results = []
    by_folder = {}
    for access in accesses:
        by_folder.setdefault(access.folder_id, []).append(access)
    for rows in by_folder.values():
        results.extend(_sync_folder(client, rows[0].folder, rows, actor=actor, dry_run=dry_run))
    return results


def _sync_folder(client, folder, rows, *, actor, dry_run):
    shared_id = folder.shared_folder_id
    try:
        if not shared_id and any(target_email(access) for access in rows):
            shared_id = client.ensure_shared(folder.path)
            if not dry_run:
                DropboxFolder.objects.filter(pk=folder.pk).update(shared_folder_id=shared_id)
                folder.shared_folder_id = shared_id
        members = client.folder_members(shared_id) if shared_id else {}
    except DropboxError as error:
        if dropbox_sharing.is_gone(error):
            return [_finish(access, _folder_gone(access, dry_run=dry_run), dry_run=dry_run) for access in rows]
        return [_finish(access, Result(access, error=str(error)), dry_run=dry_run) for access in rows]
    results = []
    for access in rows:
        try:
            result = _sync_access(client, access, shared_id, members, dry_run=dry_run)
        except DropboxError as error:
            result = Result(access, error=str(error))
        results.append(_finish(access, result, dry_run=dry_run))
        if not result.error and not dry_run:
            if result.removed:
                _log(actor, access, "remove", result.removed)
            if result.action and result.action != "remove":
                _log(actor, access, result.action, access.granted_email)
    return results


def _sync_access(client, access, shared_id, members, *, dry_run):
    target = target_email(access)
    old = access.granted_email.lower()
    result = Result(access)
    if old and old != target:
        member = members.get(old)
        if shared_id and member and member.level != "owner" and not _used_elsewhere(access, old):
            client.remove_member(shared_id, old)
            members.pop(old, None)
            result.action, result.removed = "remove", old
        access.granted_email = ""
        if not dry_run:
            _save(access, granted_email="")
    if not target:
        return result
    member = members.get(target)
    if member is None:
        # Adres zapisany przed wywołaniem: gdy Dropbox doda osobę, a odpowiedź nie dotrze (przekroczony czas,
        # błąd ponownego odczytu członków), późniejsze odebranie dostępu wciąż wie, kogo usunąć z folderu.
        access.granted_email = target
        if not dry_run:
            _save(access, granted_email=target)
        client.add_member(shared_id, target, access.level)
        member = client.folder_members(shared_id).get(target) or Member(access.level)
        members[target] = member
        result.action = "invite" if member.invitee else "grant"
    elif member.level == "owner":
        raise DropboxError("Ten adres to właściciel folderu w Dropboxie – CMS nie zmienia jego dostępu.")
    elif member.inherited:
        raise DropboxError(dropbox_sharing.message_for("no_explicit_access"), "no_explicit_access")
    elif dropbox_sharing.cms_level(member.level) != access.level:
        # „viewer_no_comment” to dla CMS-u Odczyt – bez zmiany poziomu w Dropboxie.
        client.update_member(shared_id, target, access.level)
        members[target] = Member(access.level, member.invitee)
        result.action = result.action or "update"
    access.granted_email = target
    access._invitee = members[target].invitee
    return result


def _folder_gone(access, *, dry_run):
    """Folderu nie ma w Dropboxie: odebranie dostępu jest zakończone (nikt go już nie ma), nadanie – błąd z podpowiedzią."""
    if target_email(access):
        return Result(access, error=FOLDER_GONE)
    removed, access.granted_email = access.granted_email, ""
    if removed and not dry_run:
        _save(access, granted_email="")
    return Result(access, action="remove" if removed else "", removed=removed)


def _used_elsewhere(access, email):
    """Ten sam adres ma mieć dostęp z innego wiersza tego folderu (np. dwie osoby z jednym kontem Dropbox)."""
    others = (DropboxAccess.objects.filter(folder_id=access.folder_id, granted_email__iexact=email)
              .exclude(pk=access.pk).select_related("person"))
    return any(target_email(other) == email for other in others)


def _finish(access, result, *, dry_run):
    """Stan wiersza po synchronizacji (błąd zachowuje adres, któremu dostęp mógł zostać)."""
    if dry_run:
        return result
    if result.error:
        _save(access, state=State.ERROR, last_error=result.error, synced_at=timezone.now())
        return result
    if access.granted_email:
        state = State.INVITED if getattr(access, "_invitee", False) else State.GRANTED
        error = ""
    elif access.wanted and person_is_member(access.person):
        state = State.PENDING
        error = "Brak adresu e-mail Dropboxa – uzupełnij go na ekranie osoby."
    else:
        state, error = State.REMOVED, ""
    _save(access, state=state, last_error=error, granted_email=access.granted_email, synced_at=timezone.now())
    return result


def _save(access, **fields):
    # update() zamiast save(): bez sygnału post_save, który zaplanowałby kolejną synchronizację.
    DropboxAccess.objects.filter(pk=access.pk).update(**fields)
    for name, value in fields.items():
        setattr(access, name, value)


def _log(actor, access, action, email):
    """Wpis w dzienniku aktywności: działanie, osoba, adres i folder (bez danych logowania Dropboxa)."""
    from core.activity_spool import enqueue_activity
    label = ACTION_LABELS[action]
    if action != "remove":
        label += f" ({LEVEL_LABELS[access.level]})"
    who = (str(access.person) if access.person_id else "osoba usunięta") + f" <{email}>"
    account = (getattr(actor, "email", "") or actor.get_username()) if getattr(actor, "pk", None) else "CMS (automatycznie)"
    try:
        enqueue_activity(user_id=actor.pk if getattr(actor, "pk", None) else None, actor=account[:254], method="POST",
                         action=f"Dropbox: {label}"[:255], target=f"{who}: {access.folder.path}"[:255],
                         path="/dropbox/sync_dropbox_access", status_code=200)
    except OSError:
        logger.exception("Nie udało się zapisać zmiany dostępu do Dropboxa w dzienniku aktywności.")


class _DryRun:
    """Klient próbny: odczyty idą do Dropboxa (jeśli skonfigurowany), zmiany są tylko zapisywane w planie."""

    def __init__(self, client):
        self.client = client
        self.added = {}

    def ensure_shared(self, path):
        if self.client is None:
            return "(udostępnienie)"
        metadata = self.client.folder_metadata(path)
        return dropbox_sharing._shared_id(metadata) or "(udostępnienie)"

    def folder_members(self, shared_id):
        members = {} if self.client is None or shared_id == "(udostępnienie)" else self.client.folder_members(shared_id)
        return {**members, **self.added}

    def add_member(self, shared_id, email, level):
        self.added[email] = Member(level)

    def update_member(self, shared_id, email, level):
        pass

    def remove_member(self, shared_id, email):
        pass


# Ustawienia z ekranu osoby

def set_person_access(person, levels):
    """levels: {folder: 'editor' | 'viewer' | ''}; pusty poziom odbiera dostęp. Zapis wierszy planuje synchronizację."""
    rows = {access.folder_id: access for access in person.dropbox_accesses.all()}
    for folder, level in levels.items():
        access = rows.get(folder.pk)
        if level:
            if access is None:
                DropboxAccess.objects.create(person=person, folder=folder, level=level, wanted=True)
            elif not access.wanted or access.level != level:
                access.wanted, access.level = True, level
                access.save(update_fields=["wanted", "level", "updated_at"])
        elif access is not None and access.wanted:
            access.wanted = False
            access.save(update_fields=["wanted", "updated_at"])


def folders_for(person):
    """Foldery na ekranie osoby: aktywne oraz te, w których osoba ma (albo ma mieć) dostęp."""
    query = Q(is_active=True)
    if person is not None and person.pk:
        query |= Q(accesses__person=person) & (Q(accesses__wanted=True) | ~Q(accesses__granted_email=""))
    return list(DropboxFolder.objects.filter(query).distinct().order_by("name", "pk"))


def revoke_all(person_ids, using="default"):
    """Odbiera wszystkie dostępy osób (dezaktywacja, usunięcie z zespołu) i planuje ich usunięcie w Dropboxie."""
    rows = DropboxAccess.objects.using(using).filter(person_id__in=person_ids)
    rows.filter(wanted=True).update(wanted=False)
    schedule(rows.filter(~Q(granted_email="") | ~Q(state=State.REMOVED)).values_list("pk", flat=True), using)


# Sygnały

@receiver(pre_save, sender=Person, dispatch_uid="dropbox-person-before")
def remember_person(sender, instance, raw=False, using=None, **kwargs):
    instance._dropbox_before = None
    if not raw and instance.pk:
        instance._dropbox_before = sender.objects.using(using).filter(pk=instance.pk).values(
            "is_active", "is_external", "dropbox_email").first()


@receiver(post_save, sender=Person, dispatch_uid="dropbox-person-saved")
def person_saved(sender, instance, raw=False, using=None, **kwargs):
    before = getattr(instance, "_dropbox_before", None)
    instance._dropbox_before = None
    if raw or not before:
        return
    was_member = before["is_active"] and not before["is_external"]
    if was_member and not person_is_member(instance):
        # Nieaktywny = usunięty z zespołu: wszystkie dostępy są odbierane. Ponowna aktywacja ich nie przywraca.
        revoke_all([instance.pk], using)
    elif (before["dropbox_email"] or "").strip().lower() != (instance.dropbox_email or "").strip().lower():
        rows = DropboxAccess.objects.using(using).filter(person=instance)
        schedule(rows.filter(Q(wanted=True) | ~Q(granted_email="")).values_list("pk", flat=True), using)


@receiver(pre_delete, sender=Person, dispatch_uid="dropbox-person-deleting")
def person_deleting(sender, instance, using=None, **kwargs):
    rows = DropboxAccess.objects.using(using).filter(person=instance)
    rows.filter(wanted=True).update(wanted=False)
    instance._dropbox_rows = list(rows.values_list("pk", flat=True))


@receiver(post_delete, sender=Person, dispatch_uid="dropbox-person-deleted")
def person_deleted(sender, instance, using=None, **kwargs):
    # Wiersze zostają bez osoby (SET_NULL), dopóki adres nie zostanie usunięty z folderu.
    schedule(getattr(instance, "_dropbox_rows", ()), using)


@receiver(post_save, sender=DropboxAccess, dispatch_uid="dropbox-access-saved")
def access_saved(sender, instance, raw=False, using=None, **kwargs):
    if not raw:
        schedule([instance.pk], using)
