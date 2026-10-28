"""Dopasowanie członków serwera Discorda do osób w CMS-ie po imieniu i nazwisku (propozycje do zatwierdzenia).

Nazwy porównywane są bez względu na wielkość liter i nadmiarowe spacje; polskie znaki diakrytyczne SĄ rozróżniane
(„Łukasz” ≠ „Lukasz”). Najpierw pseudonim na serwerze, w drugiej kolejności nazwa globalna i nazwa użytkownika
– tylko dla osób i członków niedopasowanych po pseudonimie. Pewne jest dopasowanie jednoznaczne w obie strony
(jedna osoba ↔ jeden członek). Nic nie jest zapisywane bez zaznaczenia przez superusera, a istniejący identyfikator
osoby zmienia tylko wyraźnie zaznaczone „zastąp”.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from people.discord_api import display_name
from people.models import Person

CERTAIN, REPLACE, AMBIGUOUS, NONE, LINKED, TAKEN = "certain", "replace", "ambiguous", "none", "linked", "taken"
LEVEL_LABELS = {"nick": "pseudonim na serwerze", "name": "nazwa globalna lub nazwa użytkownika"}


def normalize(value):
    return " ".join((value or "").split()).casefold()


def person_key(person):
    return normalize(f"{person.first_name} {person.last_name}")


@dataclass
class Candidate:
    user_id: str
    name: str  # nazwa widoczna na serwerze
    matched: str  # nazwa, po której dopasowano
    level: str  # nick albo name
    linked_to: Person = None  # osoba, która już ma ten identyfikator

    @property
    def level_label(self):
        return LEVEL_LABELS[self.level]


@dataclass
class Proposal:
    person: Person
    status: str
    candidates: list = field(default_factory=list)

    @property
    def candidate(self):
        return self.candidates[0] if self.candidates else None


@dataclass
class Matching:
    certain: list = field(default_factory=list)  # pewne (w tym REPLACE – inny zapisany identyfikator)
    ambiguous: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)  # osoby bez identyfikatora i bez kandydata
    linked: int = 0  # już połączone z tym samym kontem
    taken: list = field(default_factory=list)  # jedyny kandydat jest połączony z inną osobą
    members_left: list = field(default_factory=list)  # członkowie serwera bez osoby i bez propozycji

    def allowed(self):
        """{osoba: {ID dozwolonych do zapisu}} – tylko z propozycji pewnych i niejednoznacznych."""
        result = {}
        for proposal in self.certain + self.ambiguous:
            result[proposal.person.pk] = {c.user_id for c in proposal.candidates if c.linked_to is None}
        return result


def _index(members, names):
    index = defaultdict(list)
    for member in members:
        for name in names(member):
            key = normalize(name)
            if key:
                index[key].append(member)
    return index


def _nick(member):
    return [member.get("nick") or ""]


def _names(member):
    user = member.get("user") or {}
    return list(dict.fromkeys(n for n in (user.get("global_name"), user.get("username")) if n))


def match(members, people=None):
    """Propozycje dla członków ekipy (Person.objects.active()) i członków serwera (bez botów).

    Identyfikatory zapisane u wszystkich osób (także spoza ekipy) wykluczają konto z propozycji dla innej osoby."""
    if people is None:
        people = Person.objects.active().order_by("last_name", "first_name", "pk")
    people = list(people)
    members = [m for m in members if not (m.get("user") or {}).get("bot")]
    by_id = {str(m["user"]["id"]): m for m in members}
    linked = {person.discord_id: person for person in Person.objects.exclude(discord_id=None)}
    result = Matching()
    proposals, used_members = {}, set()
    remaining_people, remaining_members = list(people), list(members)
    for level, names in (("nick", _nick), ("name", _names)):
        index = _index(remaining_members, names)
        people_by_key = defaultdict(list)
        for person in remaining_people:
            people_by_key[person_key(person)].append(person)
        found = {}
        for person in remaining_people:
            key = person_key(person)
            hits = index.get(key, [])
            if hits:
                found[person.pk] = (person, key, hits)
        # Członek pasujący do kilku osób (ta sama nazwa albo różne nazwy członka) – niejednoznaczny w drugą stronę.
        member_uses = defaultdict(int)
        for _, _, hits in found.values():
            for member in hits:
                member_uses[str(member["user"]["id"])] += 1
        for pk, (person, key, hits) in found.items():
            candidates = [Candidate(str(m["user"]["id"]), display_name(m),
                                    next(n for n in names(m) if normalize(n) == key), level,
                                    linked.get(str(m["user"]["id"]))) for m in hits]
            unique = (len(candidates) == 1 and len(people_by_key[key]) == 1
                      and member_uses[candidates[0].user_id] == 1)
            proposals[pk] = (person, candidates, unique)
            used_members.update(c.user_id for c in candidates)
        remaining_people = [p for p in remaining_people if p.pk not in proposals]
        remaining_members = [m for m in remaining_members if str(m["user"]["id"]) not in used_members]
    for person in people:
        if person.pk not in proposals:
            if not person.discord_id:
                result.unmatched.append(Proposal(person, NONE))
            continue
        _, candidates, unique = proposals[person.pk]
        if person.discord_id and any(c.user_id == person.discord_id for c in candidates):
            result.linked += 1
        elif unique and candidates[0].linked_to is not None:
            result.taken.append(Proposal(person, TAKEN, candidates))
        elif unique:
            result.certain.append(Proposal(person, REPLACE if person.discord_id else CERTAIN, candidates))
        else:
            result.ambiguous.append(Proposal(person, AMBIGUOUS, candidates))
    result.members_left = sorted(
        (Candidate(user_id, display_name(m), "", "nick") for user_id, m in by_id.items()
         if user_id not in linked and user_id not in used_members),
        key=lambda c: normalize(c.name))
    return result


def apply(matching, chosen, replace):
    """Zapisuje wybrane identyfikatory. chosen {pk osoby: ID}, replace {pk osób z zaznaczonym „zastąp”}.

    Zwraca (zapisane osoby, błędy). Dozwolone są tylko ID z propozycji; istniejący identyfikator – tylko z „zastąp”;
    jedno konto dla jednej osoby."""
    allowed = matching.allowed()
    errors, saved = [], []
    counts = defaultdict(int)
    for user_id in chosen.values():
        counts[user_id] += 1
    with transaction.atomic():
        people = Person.objects.select_for_update().in_bulk(list(chosen))
        for pk, user_id in sorted(chosen.items()):
            person = people.get(pk)
            if person is None or user_id not in allowed.get(pk, ()):
                errors.append(f"{person or f'Osoba {pk}'}: tego konta nie ma już w propozycjach – odśwież stronę.")
                continue
            if counts[user_id] > 1:
                errors.append(f"{person}: to samo konto wybrano dla kilku osób – nie zapisano.")
                continue
            if person.discord_id == user_id:
                continue
            if person.discord_id and pk not in replace:
                errors.append(f"{person}: osoba ma już identyfikator {person.discord_id} – zaznacz „zastąp”, "
                              "żeby go zmienić.")
                continue
            if Person.objects.filter(discord_id=user_id).exclude(pk=pk).exists():
                errors.append(f"{person}: identyfikator {user_id} ma już inna osoba.")
                continue
            person.discord_id = user_id
            person.save(update_fields=["discord_id"])
            saved.append(person)
    return saved, errors
