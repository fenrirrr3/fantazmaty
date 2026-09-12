"""Polskie ścieżki panelu przy zachowaniu nazw używanych przez reverse()."""
from django.urls.resolvers import RoutePattern, URLPattern, URLResolver
from django.urls import register_converter

SEGMENTS = {
    "recruitment": "rekrutacja", "extract": "ekstrakty",
    "core": "organizacja", "anthologycorrection": "uwagi-do-antologii",
    "login": "logowanie", "logout": "wylogowanie", "password_change": "zmiana-hasla",
    "done": "gotowe", "autocomplete": "podpowiedzi", "jsi18n": "tlumaczenia",
    "auth": "konta", "user": "uzytkownicy", "group": "grupy",
    "authors": "autorzy", "author": "autor", "authornote": "notatki-autora",
    "blacklistedauthor": "czarna-lista", "blacklist": "czarna-lista",
    "texts": "teksty", "text": "tekst", "review": "recenzja",
    "reviewassignment": "przydzial-recenzji", "reviewers": "recenzenci",
    "anthology": "antologia", "anthologytask": "zadania-antologii", "textnote": "notatki-tekstu",
    "people": "zespol", "person": "osoba", "role": "rola", "vacation": "urlop",
    "workflow": "etapy", "workflowstage": "etap-pracy", "workflowroleassignment": "przypisanie-roli",
    "illustrations": "ilustracje", "illustration": "ilustracja", "coverproposal": "propozycja-okladki",
    "add": "dodaj", "change": "edytuj", "delete": "usun", "history": "historia",
    "password": "haslo", "author-data": "dane-autora", "author-details": "szczegoly-autora",
}


class PolishAppConverter:
    regex = "autorzy|teksty|zespol|etapy|ilustracje|konta|organizacja"

    def to_python(self, value):
        return {"organizacja": "core", "autorzy": "authors", "teksty": "texts", "zespol": "people",
                "etapy": "workflow", "ilustracje": "illustrations", "konta": "auth"}.get(value, value)

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
