"""Polskie ścieżki panelu przy zachowaniu nazw używanych przez reverse()."""
from django.urls.resolvers import RoutePattern, URLPattern, URLResolver
from django.urls import register_converter

SEGMENTS = {
    'postlayoutassignment': 'przydzial-poskladowej',
    'audiocontributor': 'lektor-dzwiekowiec',
    'audiobook': 'audiobook',
    'audiobookstage': 'etap-audiobooka',
    'publicaudiobooksettings': 'zewnetrzne-audiobooki',
    'publicillustrationsettings': 'zewnetrzne-ilustracje',
    'replacemententry': 'slownik-zamian',
    'notificationrule': 'powiadomienia-discord', 'discordrole': 'role-discorda',
    'discordnotification': 'wyslane-powiadomienia', 'discordrolechange': 'historia-rang-discorda',
    'discordsettings': 'ustawienia-discorda', 'socialcalendarentry': 'kalendarz-social-media',
    'siteteamsection': 'strona-www-dzialy', 'siteteamsettings': 'strona-www-ustawienia',
    'sitecataloganthology': 'szukajka-www-katalog', 'sitelistenanthology': 'sluchaj-www-antologie',
    "blacklistentry": "wpis-czarnej-listy", "workflowrepetition": "powtorzenie-etapow",
    "workflowhandoff": "przekazanie-pracy", "r": "przejdz",
    "mailboxconnection": "skrzynki-zgloszen", "useractivity": "aktywnosc-uzytkownikow",
    "workflowevent": "zdarzenia-etapow", "prepare-text": "przygotuj-tekst", "correct": "popraw",
    "recruitment": "rekrutacja", "extract": "ekstrakty", "dates": "daty",
    "core": "organizacja", "anthologycorrection": "uwagi-do-antologii",
    "login": "logowanie", "logout": "wylogowanie", "password_change": "zmiana-hasla",
    "done": "gotowe", "autocomplete": "podpowiedzi", "jsi18n": "tlumaczenia",
    "auth": "konta", "user": "uzytkownicy",
    "authors": "autorzy", "author": "autor", "authornote": "notatki-autora",
    "blacklistedauthor": "czarna-lista", "blacklist": "czarna-lista",
    "texts": "teksty", "text": "tekst", "review": "recenzja",
    "reviewassignment": "przydzial-recenzji", "reviewers": "recenzenci",
    "anthology": "antologia", "anthologytask": "zadania-antologii", "textnote": "notatki-tekstu",
    "people": "zespol", "person": "osoba", "role": "rola", "vacation": "urlop",
    "dropboxfolder": "foldery-dropbox", "dropboxaccess": "dostep-dropbox",
    "workflow": "etapy", "workflowstage": "etap-pracy", "workflowroleassignment": "przypisanie-roli",
    "illustrations": "ilustracje", "illustration": "ilustracja", "illustrator": "ilustrator", "coverproposal": "propozycja-okladki",
    "add": "dodaj", "change": "edytuj", "delete": "usun", "history": "historia",
    "password": "haslo", "author-details": "szczegoly-autora",
}


# Polska nazwa sekcji panelu → etykieta aplikacji (lista modeli aplikacji: /panel/<sekcja>/).
APP_LABELS = {SEGMENTS[label]: label for label in ("core", "authors", "texts", "people", "workflow", "illustrations",
                                                    "auth")}


class PolishAppConverter:
    regex = "|".join(APP_LABELS)

    def to_python(self, value):
        return APP_LABELS[value]

    def to_url(self, value):
        return SEGMENTS.get(value, value)


register_converter(PolishAppConverter, "polish_app")


def polish_admin_patterns(patterns):
    output = []
    for item in patterns:
        if isinstance(item, URLPattern) and item.name == "app_list":
            output.append(URLPattern(RoutePattern("<polish_app:app_label>/", name=item.name, is_endpoint=True),
                                     item.callback, item.default_args, item.name))
            continue
        if not isinstance(item.pattern, RoutePattern):
            output.append(item)
            continue
        route = "/".join(SEGMENTS.get(segment, segment) for segment in str(item.pattern).split("/"))
        pattern = RoutePattern(route, name=item.pattern.name, is_endpoint=isinstance(item, URLPattern))
        if isinstance(item, URLResolver):
            output.append(URLResolver(pattern, polish_admin_patterns(item.url_patterns),
                item.default_kwargs, item.app_name, item.namespace))
        else:
            output.append(URLPattern(pattern, item.callback, item.default_args, item.name))
    return output
