"""Shared identity validation for the directory and Django admin."""
import unicodedata
from collections import Counter
from django import forms
from .models import Illustrator


def identity_key(value):
    return ' '.join(unicodedata.normalize('NFC', value or '').split()).casefold()


def conflicting_contacts(first_name, last_name, pseudonym, pk=None):
    name, alias = identity_key(f'{first_name} {last_name}'), identity_key(pseudonym)
    conflicts = []
    for person in Illustrator.objects.exclude(pk=pk).only('pk', 'first_name', 'last_name', 'pseudonym'):
        other_name, other_alias = identity_key(str(person)), identity_key(person.pseudonym)
        if (alias and alias in (other_name, other_alias)) or (other_alias and name == other_alias):
            conflicts.append(person)
    return conflicts


def duplicate_display_names():
    counts = Counter(identity_key(person.display_name) for person in
                     Illustrator.objects.only('pk', 'first_name', 'last_name', 'pseudonym'))
    return {name for name, count in counts.items() if count > 1}


def contact_label(person, duplicates):
    return f'{person.display_name} (ID {person.pk})' if identity_key(person.display_name) in duplicates else person.display_name


class IllustratorContactForm(forms.ModelForm):
    class Meta:
        model = Illustrator
        fields = ('first_name', 'last_name', 'pseudonym', 'email', 'portfolio', 'preferences', 'covers', 'is_active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_identity = tuple(identity_key(getattr(self.instance, name)) for name in ('first_name', 'last_name', 'pseudonym'))
        if self.instance.pk:
            conflicts = conflicting_contacts(self.instance.first_name, self.instance.last_name, self.instance.pseudonym, self.instance.pk)
            if conflicts:
                self.fields['pseudonym'].help_text = 'Istnieje konflikt nazwy z wpisami ' + ', '.join(f'#{p.pk}' for p in conflicts) + '. Uporządkuj pseudonimy lub scal wpisy tej samej osoby. Pozostałe dane możesz edytować.'

    def clean(self):
        data = super().clean()
        fields = ('first_name', 'last_name', 'pseudonym')
        if not all(name in data for name in fields):
            return data
        identity = tuple(identity_key(data[name]) for name in fields)
        # Do not lock existing historical duplicates out of unrelated contact edits.
        if not self.instance.pk or identity != self._original_identity:
            conflicts = conflicting_contacts(*(data[name] for name in fields), self.instance.pk)
            if conflicts:
                self.add_error('pseudonym', 'Ta nazwa lub pseudonim koliduje z istniejącym wpisem: ' +
                               ', '.join(f'{p.display_name} (ID {p.pk})' for p in conflicts) +
                               '. Wybierz istniejący profil lub scal wpisy tej samej osoby zamiast tworzyć duplikat.')
        return data
