"""Adding a contact and assigning one anthology task without creating a login."""
from django import forms
from django.db import transaction
from django.db.models import BooleanField, ExpressionWrapper, Q
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


class AnthologyNotificationsForm(forms.ModelForm):
    """Ustawienie „Powiadomienia Discord” na stronie antologii i powieści – zmienia tylko superuser."""

    class Meta:
        model = Anthology
        fields = ('discord_notifications',)


class NewTaskPersonForm(forms.Form):
    first_name = forms.CharField(label='Nowa osoba – imię', max_length=100)
    last_name = forms.CharField(label='Nowa osoba – nazwisko', max_length=100)
    task_type = forms.ChoiceField(label='Zadanie do przypisania', choices=[('', 'Wybierz zadanie'), *TASK_CHOICES])

    def __init__(self, *args, anthology, **kwargs):
        super().__init__(*args, **kwargs)
        self.anthology = anthology
        for name in ('first_name', 'last_name'):
            self.fields[name].widget.attrs.update({'data-contact-mode': 'hint', 'data-contact-group': 'new-person',
                                                   'autocomplete': 'off'})
        self.fields['task_type'].widget.attrs['data-contact-kind-select'] = 'true'

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
            self.anthology.cover_illustrator = person  # podpis okładki (cover_author) ustawia Anthology.save
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


def _task_people_condition(selected_ids):
    """Warunek listy wykonawców zadań (``task_people``) – także do adnotacji listy wszystkich osób.

    Osoby spoza ekipy, które tylko konsultowały audiodeskrypcję, nie trafiają do wyboru wykonawców innych zadań.
    """
    from core.models import AudioDescription, AudioDescriptionNote
    controllers = AudioDescription.controllers.through.objects.filter(person__is_external=False).values('person_id')
    note_authors = AudioDescriptionNote.objects.filter(author__isnull=False).values('author_id')
    condition = (
        Q(pk__in=Person.objects.active().values('pk'))
        | Q(pk__in=AnthologyTask.objects.filter(assigned_to__isnull=False).values('assigned_to_id'))
        | Q(pk__in=controllers) | Q(user_id__in=note_authors)
    )
    selected = [pk for pk in selected_ids if pk]
    return condition | Q(pk__in=selected) if selected else condition


def task_people(*selected_ids):
    """Active team plus everyone who has already worked on anthology tasks or audio descriptions."""
    return Person.objects.filter(_task_people_condition(selected_ids)).order_by('last_name', 'first_name', 'pk')


def consultant_people(*selected_ids):
    """Wszystkie osoby – także spoza ekipy i nieaktywne – do wyboru konsultacji audiodeskrypcji.

    Adnotacja ``for_tasks`` mówi, czy osoba jest też na liście ``task_people(*selected_ids)``: jedno zapytanie
    wystarcza wtedy dla obu pól strony audiodeskrypcji.
    """
    return Person.objects.annotate(for_tasks=ExpressionWrapper(
        _task_people_condition(selected_ids), output_field=BooleanField())).order_by('last_name', 'first_name', 'pk')


class _SharedPeopleIterator(forms.models.ModelChoiceIterator):
    """Opcje z listy osób pobranej raz dla wszystkich pól strony (bez zapytania na pole)."""

    def __iter__(self):
        if self.field.empty_label is not None:
            yield ('', self.field.empty_label)
        for person in self.field.people:
            yield self.choice(person)

    def __len__(self):
        return len(self.field.people) + (self.field.empty_label is not None)

    def __bool__(self):
        return self.field.empty_label is not None or bool(self.field.people)


class _SharedPeopleMixin:
    """Pole osób, które może korzystać ze wspólnej, już pobranej listy ``task_people``.

    Po ``use_people(people)`` opcje pochodzą z listy, a queryset służy tylko walidacji wartości.
    """
    people = None

    def use_people(self, people, queryset=None):
        """Opcje z listy ``people``; ``queryset`` (domyślnie ``people``) waliduje wysłane wartości."""
        self.people = list(people)
        self.queryset = people if queryset is None else queryset

    def _get_choices(self):
        if self.people is None:
            return super()._get_choices()
        return _SharedPeopleIterator(self)

    choices = property(_get_choices, forms.ChoiceField.choices.fset)


class TaskPersonChoice(_SharedPeopleMixin, forms.ModelChoiceField):
    def label_from_instance(self, person):
        if person.is_external:
            return f'{person} – zewnętrzny'
        return f'{person}' if person.is_active else f'{person} – nieaktywny'


class TaskPeopleChoice(_SharedPeopleMixin, forms.ModelMultipleChoiceField):
    label_from_instance = TaskPersonChoice.label_from_instance


def consultant_label(person):
    """Etykieta osoby konsultacji audiodeskrypcji: osoby spoza ekipy i nieaktywne są wyraźnie oznaczone."""
    if person.is_external:
        return f'{person} – spoza ekipy'
    return f'{person}' if person.is_active else f'{person} – nieaktywny'


class ConsultantChoice(TaskPeopleChoice):
    def label_from_instance(self, person):
        return consultant_label(person)


def grouped_checklist(issues):
    groups = {}
    for item in issues:
        groups.setdefault(item['label'], []).append(item)
    return [{'label': label, 'items': items} for label, items in groups.items()]
