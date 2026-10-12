"""Wspólne narzędzia testów: konta zespołu, tokeny wersji edycji, gotowe zestawy danych.

Używaj ich zamiast kopiować pomocniki do kolejnych plików testów.
"""
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import signing
from lxml import html as lxml_html

from core.edit_versions import version_of

EDIT_VERSION_SALT = 'cms-edit-version'


def create_member(username, role_name):
    """Konto członka zespołu z profilem osoby i rolą (także grupą o tej nazwie)."""
    from people.models import Person, Role
    if role_name == 'Koordynator':
        role_name = 'Koordynator redakcji'
    user = get_user_model().objects.create_user(username=username)
    person = Person.objects.create(first_name=username, last_name='Testowy', email=f'{username}@example.com', user=user)
    role, _ = Role.objects.get_or_create(name=role_name)
    person.roles.add(role)
    group, _ = Group.objects.get_or_create(name=role_name)
    user.groups.add(group)
    return user


def create_superuser(username='admin', email=None, password='test'):
    return get_user_model().objects.create_superuser(username, email or f'{username}@example.test', password)


def edit_token(user, obj, label=None):
    """Token wersji edycji, jaki strona umieszcza w polu _edit_version formularza obiektu."""
    label = label or obj._meta.concrete_model._meta.label_lower
    return signing.dumps([user.pk, f'{label}:{obj.pk}', version_of(obj)], salt=EDIT_VERSION_SALT)


def assign_artists(illustration, artists, *, status=None, preserve_assignment_date=False):
    """Przypisuje ilustratorów tak jak formularz przypisania: walidacja całości, potem relacja wiele do wielu."""
    from django.db import transaction
    artists = list(artists)
    illustration._selected_illustrator_ids = {artist.pk for artist in artists}
    if status is not None:
        illustration.status = status
    try:
        with transaction.atomic(using=illustration._state.db):
            illustration.save(preserve_assignment_date=preserve_assignment_date)
            illustration.illustrators.set(artists)
    finally:
        del illustration._selected_illustrator_ids
    return illustration


def form_value(response, name='_edit_version', form_xpath='//'):
    """Wartość pola z wyrenderowanej strony (np. token wersji z formularza)."""
    return lxml_html.fromstring(response.content).xpath(f'{form_xpath}input[@name="{name}"]/@value')[0]


class FormControls(HTMLParser):
    """Pola, formularze i przyciski formularza o danym action (parser HTML bez zależności)."""

    def __init__(self, target):
        super().__init__()
        self.fields = {}
        self.forms = []
        self.buttons = []
        self.target = target
        self.in_target = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms.append(attrs)
            self.in_target = attrs.get("action") == self.target
        if self.in_target and tag in ("input", "select", "textarea") and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")
        if self.in_target and tag == "button":
            self.buttons.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_target = False
