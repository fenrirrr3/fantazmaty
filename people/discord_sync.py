"""Rangi Discorda według ról w CMS-ie: podgląd, synchronizacja po zmianach osób i okresowe wyrównanie.

Mapowanie to core.DiscordRole (ranga → role CMS i tryb): „wyłączona” – CMS rangi nie rusza, „tylko nadawanie” –
dodaje ją brakującym członkom ekipy, „pełna synchronizacja” (po zatwierdzeniu podglądu) – także odbiera ją osobom
z CMS-u, które nie powinny jej mieć. CMS zmienia WYŁĄCZNIE rangi z mapowania i WYŁĄCZNIE u członków serwera
połączonych z osobą w CMS-ie (identyfikator Discorda w profilu osoby); nierozpoznanych członków serwera nie rusza.

Zmiana w CMS-ie (role osoby, „wciąż w ekipie”, konto, identyfikator Discorda, usunięcie osoby) planuje synchronizację
tej osoby po zatwierdzeniu transakcji (transaction.on_commit): wycofany zapis niczego na Discordzie nie zmienia,
a błąd Discorda nie cofa zapisu – trafia do historii (core.DiscordRoleChange) jako błąd do ponowienia. Ponowienie
i wyrównanie rozjazdów robi komenda sync_discord_roles (zadanie zaplanowane) albo przycisk w panelu.
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from core.notification_models import DiscordRole, DiscordRoleChange
from people import discord_api
from people.discord_api import DiscordClient, DiscordError, display_name
from people.models import Person, Role

logger = logging.getLogger(__name__)

Mode = DiscordRole.SyncMode
Action = DiscordRoleChange.Action
Result = DiscordRoleChange.Result
Source = DiscordRoleChange.Source

# Czekanie na limit zapytań (429): w żądaniu z panelu krótko, w komendzie okresowej dłużej.
REQUEST_MAX_WAIT = 5.0
COMMAND_MAX_WAIT = 60.0

UNRECOGNIZED_RULE = ("Członków serwera z tą rangą, których CMS nie rozpoznaje (brak osoby z ich identyfikatorem), "
                     "CMS nie rusza – ani teraz, ani przy pełnej synchronizacji.")
NO_MANAGED_ROLES = ("Żadna ranga nie jest nadawana przez CMS – ustaw role CMS i tryb przy randze w „Role Discorda”.")


def is_member(person):
    """Członek ekipy w rozumieniu rang: jak Person.objects.active() – w ekipie, nie zewnętrzny, konto aktywne."""
    return bool(person and person.is_active and not person.is_external
                and (person.user_id is None or person.user.is_active))


def managed_roles(using="default"):
    """Rangi zarządzane przez CMS (tryb inny niż „wyłączona”). Ta sama ranga (ID) liczy się raz – pierwsza wygrywa."""
    roles, seen = [], set()
    for role in (DiscordRole.objects.using(using).exclude(sync_mode=Mode.OFF)
                 .prefetch_related("cms_roles").order_by("name", "pk")):
        if role.role_id not in seen:
            seen.add(role.role_id)
            roles.append(role)
    return roles


def wanted_role_ids(person, roles):
    """ID rang, które osoba powinna mieć (role osoby z prefetch_related albo zapytanie)."""
    if not is_member(person):
        return set()
    person_roles = {role.pk for role in person.roles.all()}
    return {role.role_id for role in roles if person_roles & {cms.pk for cms in role.cms_roles.all()}}


def _people(using="default"):
    return Person.objects.using(using).select_related("user").prefetch_related("roles")


# Podgląd i plan

@dataclass
class Entry:
    """Wiersz podglądu: osoba z CMS-u (może być pusta) i członek serwera (może być pusty)."""
    person: Person = None
    user_id: str = ""
    name: str = ""  # nazwa na serwerze

    @property
    def label(self):
        return str(self.person) if self.person else (self.name or self.user_id)


@dataclass
class RolePlan:
    role: DiscordRole
    mode: str
    discord_name: str = ""
    missing_on_server: bool = False
    grant: list = field(default_factory=list)
    revoke: list = field(default_factory=list)  # straciliby rangę przy pełnej synchronizacji
    no_id: list = field(default_factory=list)
    not_on_server: list = field(default_factory=list)
    unrecognized: list = field(default_factory=list)
    has: int = 0

    @property
    def applies_revoke(self):
        return self.mode == Mode.FULL

    @property
    def mode_label(self):
        return Mode(self.mode).label

    @property
    def stale_approval(self):
        """Ustawiono pełną synchronizację, ale zatwierdzenie dotyczy innego mapowania (działa tylko nadawanie)."""
        return self.role.sync_mode == Mode.FULL and self.mode != Mode.FULL

    def digest_data(self):
        revoke = sorted(entry.user_id for entry in self.revoke) if self.applies_revoke else []
        return [self.role.pk, self.role.role_id, self.mode, sorted(entry.user_id for entry in self.grant), revoke]


def guild_snapshot(client):
    """(członkowie serwera {id: członek}, rangi serwera {id: ranga}) – tylko odczyt."""
    members = {str(member["user"]["id"]): member for member in client.members()}
    return members, client.roles()


def build_plans(roles, members, guild_roles, using="default"):
    """Plan dla każdej rangi z mapowania: kto dostanie, kto straci, kogo brakuje – bez zmian na Discordzie."""
    people = list(_people(using))
    linked = {person.discord_id: person for person in people if person.discord_id}
    plans = []
    for role in roles:
        plan = RolePlan(role, role.effective_mode())
        info = guild_roles.get(role.role_id)
        plan.discord_name = (info or {}).get("name", "")
        plan.missing_on_server = info is None
        wanted = []
        for person in people:
            if role.role_id in wanted_role_ids(person, [role]):
                wanted.append(person)
        wanted_ids = {person.pk for person in wanted}
        for person in wanted:
            member = members.get(person.discord_id or "")
            if not person.discord_id:
                plan.no_id.append(Entry(person))
            elif member is None:
                plan.not_on_server.append(Entry(person, person.discord_id))
            elif role.role_id in member.get("roles", ()):
                plan.has += 1
            else:
                plan.grant.append(Entry(person, person.discord_id, display_name(member)))
        for user_id, member in members.items():
            if role.role_id not in member.get("roles", ()):
                continue
            person = linked.get(user_id)
            if person is None:
                plan.unrecognized.append(Entry(None, user_id, display_name(member)))
            elif person.pk not in wanted_ids:
                plan.revoke.append(Entry(person, user_id, display_name(member)))
        for rows in (plan.grant, plan.revoke, plan.unrecognized):
            rows.sort(key=lambda entry: entry.label.casefold())
        plans.append(plan)
    return plans


def plans_digest(plans):
    """Skrót pokazanego planu – zastosowanie z panelu wymaga tego samego stanu."""
    data = [plan.digest_data() for plan in plans]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:32]


def preview(roles=None, client=None):
    """(plany, błąd) dla podglądu w panelu. Nic nie zmienia na Discordzie ani w bazie."""
    roles = managed_roles() if roles is None else roles
    if not discord_api.is_configured() and client is None:
        return [], discord_api.NOT_CONFIGURED
    try:
        client = client or DiscordClient(max_wait=REQUEST_MAX_WAIT)
        members, guild_roles = guild_snapshot(client)
    except DiscordError as error:
        return [], str(error)
    return build_plans(roles, members, guild_roles), ""


# Wykonanie

class Runner:
    """Wykonuje operacje na Discordzie i zapisuje każdą w historii. Po 401 nic dalej; po 403 dla rangi – ta ranga
    nie jest już próbowana w tym przebiegu (błąd zapisany przy każdej pominiętej operacji)."""

    def __init__(self, client, *, source, actor=None):
        self.client = client
        self.source = source
        self.actor = actor if getattr(actor, "pk", None) else None
        self.blocked = {}  # ID rangi albo „*” → DiscordError
        self.changes = []

    def run(self, action, role, user_id, person, reason):
        error = self.blocked.get("*") or self.blocked.get(role.role_id)
        label = str(person) if person else ""
        audit = f"CMS Fantazmaty: {reason}" + (f" ({label})" if label else "")
        if error is None:
            try:
                if action == Action.GRANT:
                    self.client.add_role(user_id, role.role_id, audit)
                else:
                    self.client.remove_role(user_id, role.role_id, audit)
            except DiscordError as exception:
                error = exception
                if exception.status == 401:
                    self.blocked["*"] = exception
                elif exception.status == 403:
                    self.blocked[role.role_id] = exception
        change = DiscordRoleChange.objects.create(
            person=person, person_label=label[:210], discord_user_id=user_id, discord_role=role,
            role_label=str(role)[:60], role_id=role.role_id, action=action,
            result=Result.ERROR if error else Result.DONE, reason=reason[:255], source=self.source,
            actor=self.actor, response=(f"{error} [{error.short()}]" if error else "HTTP 204")[:300])
        change.error = str(error) if error else ""
        self.changes.append(change)
        return change

    def fail(self, action, role, user_id, person, reason, error):
        """Błąd przed operacją (np. odczyt członka) – zapis w historii bez wywołania."""
        change = DiscordRoleChange.objects.create(
            person=person, person_label=(str(person) if person else "")[:210], discord_user_id=user_id,
            discord_role=role, role_label=str(role)[:60], role_id=role.role_id, action=action, result=Result.ERROR,
            reason=reason[:255], source=self.source, actor=self.actor, response=f"{error} [{error.short()}]"[:300])
        change.error = str(error)
        self.changes.append(change)
        return change


def apply_plans(plans, runner, reason):
    """Nadaje brakującym i (tylko w pełnej synchronizacji) odbiera nadmiarowe rangi z planów."""
    for plan in plans:
        for entry in plan.grant:
            runner.run(Action.GRANT, plan.role, entry.user_id, entry.person, reason)
        if plan.applies_revoke:
            for entry in plan.revoke:
                runner.run(Action.REVOKE, plan.role, entry.user_id, entry.person, reason)
    return runner.changes


def retry_orphans(runner, roles, members, reason):
    """Ponawia nieudane odebrania rangi kontom już niepołączonym z CMS-em (usunięta osoba, zmieniony identyfikator),
    o ile ranga jest w pełnej synchronizacji. Bez tego nierozpoznany członek zostałby z rangą na zawsze."""
    full = {role.role_id: role for role in roles if role.effective_mode() == Mode.FULL}
    linked = set(Person.objects.exclude(discord_id=None).values_list("discord_id", flat=True))
    pending = (DiscordRoleChange.objects.filter(result=Result.ERROR, action=Action.REVOKE, role_id__in=full)
               .exclude(discord_user_id__in=linked).order_by("pk"))
    done = set()
    for change in pending:
        key = (change.discord_user_id, change.role_id)
        if key in done:
            continue
        done.add(key)
        member = members.get(change.discord_user_id)
        if member is not None and change.role_id in member.get("roles", ()):
            runner.run(Action.REVOKE, full[change.role_id], change.discord_user_id, None,
                       f"{reason} – ponowienie: {change.reason}")


def resolve_errors(started, changes):
    """Wcześniejsze błędy, których para (konto, ranga) nie zawiodła w tym przebiegu, są nieaktualne – ponowiono je."""
    failing = {(change.discord_user_id, change.role_id) for change in changes if change.result == Result.ERROR}
    stale = [pk for pk, user_id, role_id in DiscordRoleChange.objects.filter(
        result=Result.ERROR, created_at__lt=started).values_list("pk", "discord_user_id", "role_id")
        if (user_id, role_id) not in failing]
    DiscordRoleChange.objects.filter(pk__in=stale).update(result=Result.RETRIED)


@dataclass
class SyncOutcome:
    plans: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    error: str = ""
    stale: bool = False  # stan zmienił się od podglądu – nic nie wykonano

    @property
    def errors(self):
        return [change for change in self.changes if change.result == Result.ERROR]


def synchronize(*, source, actor=None, roles=None, client=None, expected=None, reason=None, max_wait=None):
    """Pełne wyrównanie rang z mapowania (komenda, przycisk w panelu, zatwierdzenie): plan, ponowienia, wykonanie.

    expected – skrót planu z podglądu; inny stan niż pokazany → nic nie jest wykonywane (stale)."""
    all_roles = roles is None
    roles = managed_roles() if roles is None else roles
    if client is None:
        if not discord_api.is_configured():
            return SyncOutcome(error=discord_api.NOT_CONFIGURED)
        client = DiscordClient(max_wait=max_wait or (COMMAND_MAX_WAIT if source == Source.COMMAND else REQUEST_MAX_WAIT))
    if not roles:
        return SyncOutcome(error=NO_MANAGED_ROLES)
    started = timezone.now()
    try:
        members, guild_roles = guild_snapshot(client)
    except DiscordError as error:
        return SyncOutcome(error=str(error))
    plans = build_plans(roles, members, guild_roles)
    if expected is not None and expected != plans_digest(plans):
        return SyncOutcome(plans=plans, stale=True)
    reason = reason or Source(source).label.lower()
    runner = Runner(client, source=source, actor=actor)
    if all_roles:
        retry_orphans(runner, roles, members, reason)
    apply_plans(plans, runner, reason)
    if all_roles:
        resolve_errors(started, runner.changes)
    return SyncOutcome(plans=plans, changes=runner.changes)


def approve_full_sync(role, actor, *, expected, client=None):
    """„Zatwierdź i włącz”: zapis zatwierdzenia dla bieżącego mapowania i od razu wykonanie podglądu tej rangi."""
    role = DiscordRole.objects.prefetch_related("cms_roles").get(pk=role.pk)
    if not role.cms_roles.all():
        return SyncOutcome(error=f"{role}: ranga nie ma ról w CMS-ie – pełna synchronizacja odebrałaby ją wszystkim "
                                 "rozpoznanym osobom. Najpierw wybierz role w CMS-ie.")
    role.sync_mode = Mode.FULL
    candidate = [role]
    # Plan liczony jak po włączeniu (z odbieraniem), żeby skrót odpowiadał temu, co pokazał podgląd zatwierdzenia.
    role.full_sync_approved_for = role.mapping_key()
    outcome = synchronize(source=Source.APPROVAL, actor=actor, roles=candidate, client=client, expected=expected,
                          reason="zatwierdzenie pełnej synchronizacji")
    if outcome.error or outcome.stale:
        return outcome
    DiscordRole.objects.filter(pk=role.pk).update(
        sync_mode=Mode.FULL, full_sync_approved_for=role.full_sync_approved_for,
        full_sync_approved_at=timezone.now(), full_sync_approved_by=actor if getattr(actor, "pk", None) else None)
    return outcome


def approval_preview(role, client=None):
    """Podgląd rangi tak, jakby pełna synchronizacja była już zatwierdzona (z odbieraniem) – do strony zatwierdzenia."""
    role = DiscordRole.objects.prefetch_related("cms_roles").get(pk=role.pk)
    role.sync_mode = Mode.FULL
    role.full_sync_approved_for = role.mapping_key()
    return preview([role], client=client)


# Synchronizacja wybranych osób (po zmianach w CMS-ie)

def sync_targets(person_ids, orphans, reasons, *, actor=None, client=None, using="default"):
    """Uzgadnia rangi wskazanych osób (i odbiera je kontom odłączonym: orphans {ID: (etykieta, powód)}).

    Jedno zapytanie o członka na konto, potem tylko brakujące zmiany. Zwraca listę wpisów historii."""
    roles = managed_roles(using)
    if not roles or not (person_ids or orphans):
        return []
    client = client or DiscordClient(max_wait=REQUEST_MAX_WAIT)
    runner = Runner(client, source=Source.CHANGE, actor=actor)
    people = {person.pk: person for person in _people(using).filter(pk__in=person_ids).exclude(discord_id=None)}
    linked = set(Person.objects.using(using).exclude(discord_id=None).values_list("discord_id", flat=True))
    targets = [(person.discord_id, person, wanted_role_ids(person, roles), "; ".join(sorted(reasons.get(pk, ()))))
               for pk, person in sorted(people.items())]
    targets += [(user_id, None, set(), reason) for user_id, (label, reason) in sorted(orphans.items())
                if user_id not in linked]
    for user_id, person, wanted, reason in targets:
        reason = reason or "zmiana w CMS-ie"
        if person is None:
            reason = f"{reason} ({orphans[user_id][0]})"
        _sync_member(runner, roles, user_id, person, wanted, reason)
    return runner.changes


def _sync_member(runner, roles, user_id, person, wanted, reason):
    full = [role for role in roles if role.effective_mode() == Mode.FULL]
    try:
        member = runner.client.member(user_id)
    except DiscordError as error:
        # Nie wiadomo, co na Discordzie – zapis błędu przy rangach, które mogły wymagać zmiany (ponowi komenda).
        for role in roles:
            if role.role_id in wanted:
                runner.fail(Action.GRANT, role, user_id, person, reason, error)
            elif role in full:
                runner.fail(Action.REVOKE, role, user_id, person, reason, error)
        return
    if member is None:
        return  # osoby nie ma na serwerze – nie ma czego nadawać ani odbierać
    has = set(member.get("roles", ()))
    for role in roles:
        if role.role_id in wanted and role.role_id not in has:
            runner.run(Action.GRANT, role, user_id, person, reason)
        elif role.role_id not in wanted and role.role_id in has and role in full:
            runner.run(Action.REVOKE, role, user_id, person, reason)


def describe(change):
    """Komunikat dla panelu admina o jednym wpisie historii."""
    who = change.person_label or f"konto {change.discord_user_id}"
    verb = "nadano rangę" if change.action == Action.GRANT else "odebrano rangę"
    if change.result == Result.ERROR:
        verb = "nie udało się " + ("nadać rangi" if change.action == Action.GRANT else "odebrać rangi")
        return f"Discord – {who}: {verb} {change.role_label}. {getattr(change, 'error', '') or change.response}"
    return f"Discord – {who}: {verb} {change.role_label}."


# Planowanie po zatwierdzeniu transakcji (jak people.dropbox_access)

_PENDING_ATTR = "_discord_roles_batch"


class _Batch:
    def __init__(self, using):
        self.using = using
        self.people = {}  # pk → {powody}
        self.orphans = {}  # ID Discorda → (etykieta, powód)
        self.actor = None
        self.notifiers = []
        self.registered = False

    def register(self):
        if not self.registered:
            self.registered = True
            transaction.on_commit(self.flush, using=self.using)

    def is_registered(self, connection):
        return any(entry[1] == self.flush for entry in connection.run_on_commit)

    def flush(self):
        connection = connections[self.using]
        if getattr(connection, _PENDING_ATTR, None) is self:
            delattr(connection, _PENDING_ATTR)
        if not (self.people or self.orphans) or not discord_api.is_configured():
            return
        try:
            changes = sync_targets(set(self.people), self.orphans, self.people, actor=self.actor, using=self.using)
        except Exception:
            # Błąd Discorda nie może zamienić zapisanej już zmiany w błąd 500.
            logger.exception("Synchronizacja rang Discorda nie powiodła się.")
            changes = []
        for notify in self.notifiers:
            notify(changes)


def _batch(using):
    connection = connections[using]
    batch = getattr(connection, _PENDING_ATTR, None)
    if batch is not None and batch.is_registered(connection):
        return batch
    batch = _Batch(using)
    setattr(connection, _PENDING_ATTR, batch)
    return batch


def schedule(person_ids, reason, using="default"):
    """Synchronizacja osób po zatwierdzeniu transakcji (poza transakcją – od razu)."""
    person_ids = [pk for pk in person_ids if pk]
    if not person_ids or not discord_api.is_configured():
        return  # bez bota nie ma czego synchronizować – rozjazdy wyrówna komenda po konfiguracji
    batch = _batch(using)
    for pk in person_ids:
        batch.people.setdefault(pk, set()).add(reason)
    batch.register()


def schedule_orphan(user_id, label, reason, using="default"):
    """Odebranie rang z mapowania (w pełnej synchronizacji) kontu odłączonemu od osoby."""
    if not user_id or not discord_api.is_configured():
        return
    batch = _batch(using)
    batch.orphans[user_id] = (label, reason)
    batch.register()


def report_to(request, notify, using="default"):
    """Wyniki synchronizacji z bieżącej transakcji trafią do notify(changes); zmiany zapisze się na konto z request.

    Wywołuj w transakcji przed zapisem (np. w save_model admina), żeby partia nie wykonała się wcześniej."""
    batch = _batch(using)
    batch.actor = getattr(request, "user", None)
    batch.notifiers.append(notify)
    batch.register()


def admin_messages(model_admin, request):
    """notify dla report_to: komunikaty panelu admina (nadania, odebrania i błędy)."""
    from django.contrib import messages

    def notify(changes):
        for change in changes:
            level = messages.ERROR if change.result == Result.ERROR else messages.SUCCESS
            model_admin.message_user(request, describe(change), level)
    return notify


# Sygnały

@receiver(post_save, sender=Person, dispatch_uid="discord-person-saved")
def person_saved(sender, instance, raw=False, using=None, created=False, **kwargs):
    before = getattr(instance, "_previous_row", None)  # people.models.remember_person_row
    if raw or not before:
        return  # nowa osoba – rangi wynikają z ról, dodawanych po zapisie (m2m_changed)
    old_id, new_id = before.get("discord_id") or None, instance.discord_id or None
    if old_id != new_id:
        schedule_orphan(old_id, str(instance), "zmiana identyfikatora Discorda osoby", using)
        schedule([instance.pk], "zmiana identyfikatora Discorda", using)
    was_member = before["is_active"] and not before["is_external"]
    now_member = instance.is_active and not instance.is_external
    if was_member != now_member:
        schedule([instance.pk], "powrót do ekipy" if now_member else "odejście z ekipy (dezaktywacja)", using)


@receiver(m2m_changed, sender=Person.roles.through, dispatch_uid="discord-person-roles")
def person_roles_changed(sender, instance, action, reverse, pk_set, using=None, **kwargs):
    if action == "pre_clear" and reverse:
        instance._discord_cleared_people = list(instance.people.values_list("pk", flat=True))
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        schedule([instance.pk], "zmiana ról osoby", using)
    else:
        people = getattr(instance, "_discord_cleared_people", []) if action == "post_clear" else pk_set or ()
        schedule(people, f"zmiana osób z rolą „{instance.name}”", using)


@receiver(post_delete, sender=Role, dispatch_uid="discord-role-deleted")
def role_deleted(sender, instance, using=None, **kwargs):
    # Osoby roli sprzed usunięcia: people.models.remember_role_people.
    schedule(getattr(instance, "_role_people", ()), f"usunięcie roli „{instance.name}” z CMS-u", using)


@receiver(pre_delete, sender=Person, dispatch_uid="discord-person-deleting")
def person_deleting(sender, instance, using=None, **kwargs):
    instance._discord_deleted = (instance.discord_id, str(instance))


@receiver(post_delete, sender=Person, dispatch_uid="discord-person-deleted")
def person_deleted(sender, instance, using=None, **kwargs):
    user_id, label = getattr(instance, "_discord_deleted", (None, ""))
    schedule_orphan(user_id, label, "usunięcie osoby z CMS-u", using)


@receiver(post_save, sender=get_user_model(), dispatch_uid="discord-user-saved")
def user_saved(sender, instance, raw=False, created=False, update_fields=None, using=None, **kwargs):
    """Blokada lub odblokowanie konta zmienia członkostwo (jak Person.objects.active())."""
    if raw or created or (update_fields is not None and "is_active" not in update_fields):
        return
    if not discord_api.is_configured():
        return  # bez zapytania o osobę przy każdym zapisie konta
    person = (Person.objects.using(using).filter(user_id=instance.pk).exclude(discord_id=None)
              .values_list("pk", flat=True).first())
    if person:
        schedule([person], "zmiana aktywności konta", using)
