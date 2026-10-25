"""Przejęcie obecnych członków folderów Dropbox: wiersze DropboxAccess dla osób, które już mają dostęp, bez nadawania
go na nowo (żadnych zaproszeń ani zmian poziomu w Dropboxie).

Najpierw podgląd (plan_folders – odczyt członków z Dropboxa, nic nie jest zapisywane), potem zastosowanie tego samego
planu (digest jak w core.site_catalog.plan_digest). Od przejęcia CMS zarządza dostępem jak nadanym: odznaczenie
„wciąż w ekipie” albo „Brak dostępu” usuwa adres z folderu.

Dopasowanie członka do osoby:
- po „adresie e-mail Dropboxa” osoby (bez wielkości liter) – przejęcie bez pytania;
- po innym adresie osoby (e-mail kontaktowy, e-mail konta logowania) – tylko gdy administrator zaznaczy „ustaw ten
  adres jako e-mail Dropbox” (domyślnie odznaczone), i tylko osobie bez adresu Dropbox;
- adres pasujący do kilku osób, dostęp przez folder nadrzędny, poziom bez odpowiednika w CMS-ie i wpisy w CMS-ie
  w innym stanie trafiają tylko do raportu.
"""
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from core.filtering import positive_id
from core.inflection import count_text
from people import dropbox_sharing
from people.dropbox_access import LEVEL_LABELS, _log, person_is_member, report_to, schedule, state_digest
from people.dropbox_sharing import DropboxClient, DropboxError
from people.models import DropboxAccess, DropboxFolder, Person

State = DropboxAccess.State
ACCESS_FORMS = ("dostęp", "dostępy", "dostępów")

GROUPS = (
    ("import", "Do przejęcia",
     "Osoby z ekipy, które mają dostęp w Dropboxie, a w CMS-ie nie mają go jeszcze zapisanego. Przejęcie zapisze "
     "dostęp z poziomem z Dropboxa (stan „Nadany”) – bez wysyłania zaproszeń."),
    ("invited", "Zaproszenia bez konta",
     "Osoby zaproszone do folderu, które nie przyjęły jeszcze zaproszenia. Przejęcie zapisze stan „Zaproszenie wysłane”."),
    ("other_email", "Dopasowanie po innym adresie",
     "Adres z Dropboxa to e-mail kontaktowy albo e-mail konta osoby, a nie jej „adres e-mail Dropboxa”. Przejęcie "
     "tylko po zaznaczeniu – adres zostanie wtedy zapisany jako e-mail Dropbox osoby."),
    ("level", "Zarządzane przez CMS – inny poziom",
     "Przejęcie ustawi w CMS-ie poziom z Dropboxa (w Dropboxie nic się nie zmieni)."),
    ("managed", "Zarządzane przez CMS – bez zmian", ""),
    ("inactive", "Osoby spoza ekipy z dostępem",
     "Osoby nieaktywne albo zewnętrzne, które wciąż mają dostęp do folderu. Bez zaznaczenia nic się nie zmienia; "
     "zaznaczenie „odbierz dostęp” usuwa je z folderu w Dropboxie."),
    ("problems", "Do wyjaśnienia (bez zmian)", "Tych adresów CMS nie przejmie – wyjaśnij je ręcznie."),
    ("outside", "Adresy spoza bazy (bez zmian)",
     "Adresów nie ma przy żadnej osobie w CMS-ie. CMS ich nie zmienia i nie usuwa z folderu."),
)
GROUP_LABELS = {name: label for name, label, _ in GROUPS}


@dataclass
class Entry:
    """Członek folderu w Dropboxie i jego miejsce w planie."""
    group: str
    email: str
    member: dropbox_sharing.Member
    key: str
    person: Person | None = None
    access: DropboxAccess | None = None
    level: str = ""  # poziom CMS-u odpowiadający poziomowi z Dropboxa
    note: str = ""
    by_other_email: bool = False

    @property
    def dropbox_level(self):
        return dropbox_sharing.DROPBOX_LEVEL_LABELS.get(self.member.level, self.member.level)

    @property
    def cms_level(self):
        return LEVEL_LABELS.get(self.access.level, "") if self.access else ""

    def signature(self):
        access = self.access
        person = self.person
        return [self.group, self.email, self.member.level, self.member.invitee, self.member.inherited,
                person and [person.pk, person.dropbox_email, person.is_active, person.is_external],
                access and [access.pk, access.state, access.level, access.wanted, access.granted_email]]


@dataclass
class FolderPlan:
    folder: DropboxFolder
    shared_id: str = ""
    members: dict | None = None  # None – folder nieudostępniony
    error: str = ""
    owners: list = field(default_factory=list)
    entries: list = field(default_factory=list)

    @property
    def not_shared(self):
        return not self.error and self.members is None

    def groups(self):
        """[(nazwa, etykieta, opis, wpisy)] – tylko niepuste grupy, w kolejności GROUPS."""
        return [(name, label, help_text, rows) for name, label, help_text in GROUPS
                if (rows := [entry for entry in self.entries if entry.group == name])]

    def signature(self):
        return [self.folder.pk, self.shared_id, self.error, self.members is None,
                [entry.signature() for entry in self.entries]]


# Podgląd

def plan_folders(folders, client=None):
    """Plany przejęcia folderów (odczyt z Dropboxa, bez zapisu). Błąd Dropboxa trafia do planu folderu.
    Brak konfiguracji – DropboxNotConfigured z konstruktora klienta."""
    if client is None:
        # Jawnie (a nie tylko w konstruktorze klienta): testy podmieniają DropboxClient.
        if not dropbox_sharing.is_configured():
            raise dropbox_sharing.DropboxNotConfigured(dropbox_sharing.NOT_CONFIGURED, "not_configured")
        client = DropboxClient()
    plans = []
    for folder in folders:
        plan = FolderPlan(folder)
        try:
            plan.shared_id = folder.shared_folder_id or dropbox_sharing._shared_id(client.folder_metadata(folder.path))
            if plan.shared_id:
                plan.members = client.folder_members(plan.shared_id)
        except DropboxError as error:
            plan.error = str(error)
        plans.append(build_plan(plan))
    return plans


def build_plan(plan):
    """Wpisy planu z członków odczytanych z Dropboxa i stanu bazy (bez zapytań do Dropboxa)."""
    plan.entries, plan.owners = [], []
    if plan.error or not plan.members:
        return plan
    people = list(Person.objects.select_related("user"))
    by_dropbox, by_other = {}, {}
    for person in people:
        address = (person.dropbox_email or "").strip().lower()
        if address:
            by_dropbox.setdefault(address, []).append(person)
        others = {(person.email or "").strip().lower(), (getattr(person.user, "email", "") or "").strip().lower()}
        for address in others - {""}:
            by_other.setdefault(address, []).append(person)
    rows = {access.person_id: access for access in DropboxAccess.objects.filter(folder=plan.folder, person__isnull=False)}
    matches = {}
    for email, member in sorted(plan.members.items()):
        if member.level == "owner":
            plan.owners.append(email)
            continue
        entry = Entry("", email, member, f"{plan.folder.pk}:{email}")
        direct, other = by_dropbox.get(email, []), by_other.get(email, [])
        candidates = direct or other
        if len(candidates) > 1:
            entry.group = "problems"
            entry.note = "Adres pasuje do kilku osób: " + ", ".join(sorted(str(person) for person in candidates)) + "."
        elif not candidates:
            entry.group = "outside"
        else:
            entry.person, entry.by_other_email = candidates[0], not direct
            entry.access = rows.get(entry.person.pk)
            matches.setdefault(entry.person.pk, []).append(entry)
        plan.entries.append(entry)
    for found in matches.values():
        for entry in found:
            _classify(entry, others=[other.email for other in found if other is not entry])
    return plan


def _classify(entry, *, others):
    member, person, access = entry.member, entry.person, entry.access
    entry.level = dropbox_sharing.cms_level(member.level)
    if member.inherited:
        entry.group, entry.note = "problems", "Dostęp przez folder nadrzędny – CMS nim nie zarządza."
        return
    if not person_is_member(person):
        entry.group = "inactive"
        entry.level = entry.level or (access.level if access else DropboxAccess.Level.VIEWER)
        entry.note = "Zewnętrzny." if person.is_external else "Nie jest już w ekipie."
        if entry.by_other_email:
            entry.note += " Dopasowanie po innym adresie osoby."
        return
    if not entry.level:
        entry.group = "problems"
        entry.note = f"Poziom w Dropboxie: {entry.dropbox_level} – CMS obsługuje tylko Edycję i Odczyt."
        return
    if others:
        entry.group = "problems"
        entry.note = "Osoba ma w folderze także adres " + ", ".join(others) + " – zostaw jeden."
        return
    dropbox_email = (person.dropbox_email or "").strip().lower()
    if entry.by_other_email and dropbox_email:
        entry.group, entry.note = "problems", f"Osoba ma w CMS-ie inny adres Dropbox ({dropbox_email})."
        return
    granted = access.granted_email.lower() if access else ""
    if access and access.wanted and access.state in (State.GRANTED, State.INVITED) and granted == entry.email:
        entry.group = "managed" if access.level == entry.level else "level"
        if entry.group == "level":
            entry.note = f"W Dropboxie {entry.dropbox_level}, w CMS-ie {entry.cms_level}."
        return
    if access and (access.state not in (State.PENDING, State.REMOVED, State.ERROR) or granted not in ("", entry.email)):
        entry.group = "problems"
        entry.note = f"Wpis w CMS-ie: {access.get_state_display()}" + (f" ({granted})" if granted else "") + \
            " – synchronizuj ponownie albo wyjaśnij ręcznie."
        return
    entry.group = "other_email" if entry.by_other_email else ("invited" if member.invitee else "import")
    if access and access.level != entry.level:
        entry.note = f"W Dropboxie {entry.dropbox_level}, w CMS-ie {entry.cms_level}."


def plans_digest(plans):
    """Skrót planów: zastosowanie wymaga tego samego stanu Dropboxa i bazy co podgląd."""
    return state_digest([plan.signature() for plan in plans])


# Zastosowanie

def apply_plans(plans, *, set_email=(), revoke=(), actor=None, request=None, notify=None, expected=None):
    """Stosuje plany w jednej transakcji: ponownie liczy je ze stanu bazy (członkowie z podglądu) i – gdy podano
    expected – przerywa bez zmian, jeśli skrót się nie zgadza. Zwraca (plany, {folder.pk: liczba przejętych})
    albo (plany, None) przy niezgodnym skrócie.

    Bez wywołań add/update w Dropboxie; zaznaczeni z grupy „inactive” są usuwani z folderu przez zwykłą
    synchronizację (po zatwierdzeniu transakcji, wyniki – notify)."""
    set_email, revoke = set(set_email), set(revoke)
    with transaction.atomic():
        list(DropboxFolder.objects.select_for_update().filter(pk__in=[plan.folder.pk for plan in plans]))
        plans = [build_plan(plan) for plan in plans]
        if expected is not None and plans_digest(plans) != expected:
            return plans, None
        if notify:
            report_to(request, notify)
        counts, to_revoke, now = {}, [], timezone.now()
        for plan in plans:
            if plan.shared_id and not plan.folder.shared_folder_id:
                DropboxFolder.objects.filter(pk=plan.folder.pk).update(shared_folder_id=plan.shared_id)
                plan.folder.shared_folder_id = plan.shared_id
            counts[plan.folder.pk] = 0
            for entry in plan.entries:
                if entry.group == "other_email" and entry.key in set_email:
                    Person.objects.filter(pk=entry.person.pk).update(dropbox_email=entry.email)
                elif entry.group not in ("import", "invited", "level"):
                    if entry.group == "inactive" and entry.key in revoke:
                        to_revoke.append(_save_row(plan, entry, now, wanted=False))
                    continue
                _save_row(plan, entry, now, wanted=True)
                counts[plan.folder.pk] += 1
                taken = DropboxAccess(person=entry.person, folder=plan.folder, level=entry.level)
                transaction.on_commit(lambda taken=taken, email=entry.email: _log(actor, taken, "import", email))
        # Odebranie: wiersz z adresem z Dropboxa i „nie nadawać” – synchronizacja usunie adres z folderu.
        schedule(to_revoke)
    return plans, counts


def _save_row(plan, entry, now, *, wanted):
    """Wiersz dostępu z adresem i poziomem z Dropboxa; update/bulk_create bez sygnałów (bez synchronizacji)."""
    if wanted:
        state = State.INVITED if entry.member.invitee else State.GRANTED
    else:
        state = State.TO_REMOVE
    fields = {"level": entry.level, "wanted": wanted, "state": state, "granted_email": entry.email,
              "last_error": "", "updated_at": now}
    if wanted:
        fields["synced_at"] = now
    if entry.access:
        DropboxAccess.objects.filter(pk=entry.access.pk).update(**fields)
        return entry.access.pk
    DropboxAccess.objects.bulk_create([DropboxAccess(person=entry.person, folder=plan.folder, **fields)])
    return DropboxAccess.objects.get(person=entry.person, folder=plan.folder).pk


def summary(plan, count, *, planned=False):
    """Komunikat po przejęciu w folderze (planned – podgląd: liczba dostępów do przejęcia)."""
    if plan.error:
        return f"Folder {plan.folder.name}: {plan.error}"
    if plan.not_shared:
        return f"Folder {plan.folder.name} nie jest udostępniony w Dropboxie – brak członków do przejęcia."
    if planned:
        return f"Do przejęcia: {count_text(count, ACCESS_FORMS)} w folderze {plan.folder.name}."
    return f"Przejęto {count_text(count, ACCESS_FORMS)} w folderze {plan.folder.name}."


def folders_by_ids(ids):
    pks = [pk for pk in map(positive_id, ids) if pk]
    return list(DropboxFolder.objects.filter(pk__in=pks).order_by("name", "pk"))
