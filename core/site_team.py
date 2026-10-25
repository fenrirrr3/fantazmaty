"""Dane strony „Ekipa” na fantazmaty.pl: ten sam słownik dla endpointu i podglądu w zakładce „Strona WWW”.

Do JSON-a trafiają wyłącznie imiona i nazwiska – nigdy e-maile, konta ani role wewnętrzne.
"""

from core.inflection import count_text
from core.site_models import SiteTeamGroup, SiteTeamSection, SiteTeamSettings, with_version
from core.site_note import clean_note
from core.sort_keys import text_key
from people import roles
from people.models import Person

# Role wykonawcze, których brak w działach nie jest ostrzeżeniem (nie pokazujemy ich na stronie).
HIDDEN_ROLES = frozenset({roles.TRANSLATOR, roles.SOCIAL_MEDIA, roles.EMPEROR})
PEOPLE_FORMS = ('osoba', 'osoby', 'osób')


def people_by_role():
    """{nazwa roli: [(klucz sortowania, imię i nazwisko)]} dla aktywnych, niezewnętrznych osób."""
    by_role = {}
    rows = Person.objects.filter(is_active=True, is_external=False).values_list(
        'pk', 'first_name', 'last_name', 'roles__name')
    for pk, first_name, last_name, role in rows:
        if role:
            by_role.setdefault(role, {})[pk] = ((text_key(last_name), text_key(first_name), pk),
                                                f'{first_name} {last_name}'.strip())
    return by_role


def _names_for_roles(by_role, role_names):
    """Nazwiska osób z którąkolwiek z ról, bez duplikatów, według nazwiska i imienia."""
    people = {}
    for role in role_names:
        people.update(by_role.get(role, {}))
    return [name for _, name in sorted(people.values())]


def _with_manual(names, manual):
    """Nazwiska z ról, a za nimi wpisane ręcznie w podanej kolejności; bez powtórzeń."""
    return list(dict.fromkeys([*names, *manual]))


def team_sections(include_disabled=False, by_role=None):
    """Działy z grupami i nazwiskami; ``include_disabled`` dołącza też wyłączone (dla podglądu). ``by_role`` – wynik
    ``people_by_role()``, jeśli już jest pobrany."""
    by_role = people_by_role() if by_role is None else by_role
    sections = SiteTeamSection.objects.prefetch_related('coordinator_roles', 'groups__roles')
    if not include_disabled:
        sections = sections.filter(is_enabled=True)
    result = []
    for section in sections:
        groups = []
        for group in section.groups.all():
            if group.source == SiteTeamGroup.Source.MANUAL:
                names = group.manual_list
            else:
                names = _names_for_roles(by_role, [role.name for role in group.roles.all()])
            groups.append({'tytul': group.title, 'zrodlo': group.source, 'osoby': names})
        unique = len(dict.fromkeys(name for group in groups for name in group['osoby']))
        result.append({
            'slug': section.slug, 'kotwica': section.anchor, 'tytul': section.title,
            'wlaczony': section.is_enabled,
            'w_nawigacji': section.in_navigation, 'etykieta_nawigacji': section.navigation_label or section.title,
            'liczba': unique if section.show_count else None,
            'liczba_tekst': count_text(unique, PEOPLE_FORMS) if section.show_count else '',
            'koordynacja': _with_manual(
                _names_for_roles(by_role, [role.name for role in section.coordinator_roles.all()]),
                section.manual_coordinator_list),
            'notka': clean_note(section.note),
            'grupy': groups,
        })
    return result


def team_payload():
    """Słownik wysyłany stronie: osoby z ustawień, włączone działy (nazwiska na bieżąco z ``Person``) i ``wersja``
    (skrót treści)."""
    settings = SiteTeamSettings.load()
    sections = []
    for section in team_sections():
        sections.append({key: value for key, value in section.items() if key != 'wlaczony'})
        sections[-1]['grupy'] = [{'tytul': group['tytul'], 'osoby': group['osoby']} for group in section['grupy']]
    return with_version({'prowadzacy': settings.lead_editor, 'strona': settings.site_person, 'dzialy': sections})


def team_warnings(sections, by_role):
    """Ostrzeżenia podglądu: osoby z rolą wykonawczą poza działami i działy z ról bez osób (``by_role`` – wynik
    ``people_by_role()`` użyty do zbudowania działów)."""
    listed = {name for section in sections if section['wlaczony']
              for group in section['grupy'] for name in group['osoby']}
    listed |= {name for section in sections if section['wlaczony'] for name in section['koordynacja']}
    missing = {}
    for role, people in by_role.items():
        if role in HIDDEN_ROLES or roles.is_coordinator_role_name(role):
            continue
        for (last_name, first_name, _), name in people.values():
            if name not in listed:
                missing.setdefault((last_name, first_name, name), set()).add(role)
    missing_people = [f'{key[2]} ({", ".join(sorted(role_names))})' for key, role_names in sorted(missing.items())]
    empty = [section['tytul'] for section in sections if section['wlaczony'] and not section['notka']
             and any(group['zrodlo'] == SiteTeamGroup.Source.ROLES and not group['osoby']
                     for group in section['grupy'])]
    return {'missing_people': missing_people, 'empty_sections': empty}
