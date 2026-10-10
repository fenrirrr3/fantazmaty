"""Adding a contact and assigning one anthology task without creating a login."""
from django import forms
from django.db import transaction
from django.core.exceptions import ValidationError

from core.permissions import require_coordinator
from illustrations.contact_forms import identity_key
from illustrations.models import Illustrator
from people.models import Person
from texts.cover_forms import CoverAssignmentForm, matching_artists
from texts.models import Anthology, AnthologyTask


TASK_CHOICES = [
    ('banners', 'Banery'), ('blurb', 'Blurb'), ('typesetting', 'Skład'),
    ('cover_typography', 'Typografia okładki'), ('audio_description', 'Audiodeskrypcja'),
    ('cover', 'Okładka'),
]


class CompactCoverAssignmentForm(CoverAssignmentForm):
    """The anthology page has a separate contact-creation form."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop('new_cover_first_name')
        self.fields.pop('new_cover_last_name')


class NewTaskPersonForm(forms.Form):
    first_name = forms.CharField(label='Nowa osoba – imię', max_length=100)
    last_name = forms.CharField(label='Nowa osoba – nazwisko', max_length=100)
    task_type = forms.ChoiceField(label='Zadanie do przypisania', choices=[('', 'Wybierz zadanie'), *TASK_CHOICES])

    def __init__(self, *args, anthology, **kwargs):
        super().__init__(*args, **kwargs)
        self.anthology = anthology

    def clean(self):
        data = super().clean()
        first = ' '.join(data.get('first_name', '').split())
        last = ' '.join(data.get('last_name', '').split())
        data.update(first_name=first, last_name=last)
        kind = data.get('task_type')
        if not (first and last and kind):
            return data
        if kind == 'cover':
            matches = matching_artists(first, last)
            occupied = bool(self.anthology.cover_illustrator_id or self.anthology.cover_author.strip()
                            or self.anthology.cover_status != 'not_started')
        else:
            key = identity_key(f'{first} {last}')
            matches = [p for p in Person.objects.only('pk', 'first_name', 'last_name')
                       if identity_key(str(p)) == key]
            task = self.anthology.production_tasks.filter(task_type=kind).first()
            occupied = bool(task and (task.assigned_to_id or task.status != 'not_commissioned'))
        if matches:
            self.add_error('first_name', 'Taka osoba już jest w odpowiednim spisie. Wybierz ją w sekcji zadania zamiast tworzyć drugi wpis.')
        if occupied:
            self.add_error('task_type', 'To zadanie ma już wykonawcę albo zostało zlecone lub zakończone. Zmień przypisanie w jego sekcji.')
        return data

    @transaction.atomic
    def save(self, *, user):
        require_coordinator(user)
        if not self.is_valid():
            raise ValidationError('Popraw dane nowej osoby.')
        # Serialize task updates with the other anthology forms. Validate again
        # under the aggregate lock, including previously checked duplicate names.
        self.anthology = Anthology.objects.select_for_update().get(pk=self.anthology.pk)
        self.full_clean()
        if not self.is_valid():
            raise ValidationError('Dane zadania lub spisu osób zmieniły się. Sprawdź wskazane pola.')
        first, last, kind = (self.cleaned_data[k] for k in ('first_name', 'last_name', 'task_type'))
        if kind == 'cover':
            person = Illustrator(first_name=first, last_name=last, email=None, is_active=False, covers=True)
            person.full_clean()
            person.save()
            self.anthology.cover_illustrator = person
            self.anthology.cover_author = str(person)
            self.anthology.cover_status = 'in_progress'
            self.anthology.save(update_fields=['cover_illustrator', 'cover_author', 'cover_status'])
        else:
            person = Person(first_name=first, last_name=last, email=None, user=None, is_external=True, is_active=False)
            person.full_clean()
            person.save()
            task, _ = AnthologyTask.objects.get_or_create(anthology=self.anthology, task_type=kind)
            task.assigned_to = person
            task.status = AnthologyTask.Status.COMMISSIONED
            task.full_clean()
            task.save()
        return person


def grouped_checklist(issues):
    groups = {}
    for item in issues:
        groups.setdefault(item['label'], []).append(item)
    return [{'label': label, 'items': items} for label, items in groups.items()]
