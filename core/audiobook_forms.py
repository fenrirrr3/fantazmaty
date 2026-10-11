from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from core.audiobook_models import Audiobook


def eligible_proofreaders():
    return get_user_model().objects.filter(is_active=True, person_profile__is_active=True, person_profile__is_external=False,
        person_profile__roles__name__iexact='Korektor audiobooków').distinct()


class ProofreaderChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        profile = getattr(obj, 'person_profile', None)
        return str(profile) if profile else (obj.get_full_name() or obj.username)


class AudiobookProductionForm(forms.ModelForm):
    proofreader = ProofreaderChoice(queryset=get_user_model().objects.none(), required=False,
        label='Korektor audiobooka', empty_label='Nie przypisano')

    class Meta:
        model = Audiobook
        exclude = ('text',)
        widgets = {'premiere_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Preserve a former member in existing assignments, but do not offer
        # inactive accounts for new assignments.
        if 'proofreader' not in self.fields:
            return  # pole tylko do odczytu (np. poza etapem Korekta w panelu)
        ids = eligible_proofreaders().values('pk')
        self.fields['proofreader'].queryset = get_user_model().objects.filter(
            Q(pk__in=ids) | Q(pk=self.instance.proofreader_id)
        ).select_related('person_profile').order_by('last_name', 'first_name', 'pk')


class ContactChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        label = f'{obj.name} ({obj.email})' if obj.email else obj.name
        return label if obj.is_active else f'{label} – nieaktywny'


class AudiobookPeopleForm(forms.ModelForm):
    """Lektor i montaż ze spisu (jak ilustratorzy); osobę spoza spisu dopisuje się do spisu przy zapisie."""
    ROLES = {'narrator': 'lektora', 'engineer': 'montażysty'}
    new_narrator_name = forms.CharField(label='Nowy lektor – imię i nazwisko', max_length=255, required=False,
        help_text='Tylko gdy osoby nie ma w spisie (także wśród nieaktywnych).')
    new_narrator_email = forms.EmailField(label='Nowy lektor – e-mail', required=False)
    new_engineer_name = forms.CharField(label='Nowy montażysta – imię i nazwisko', max_length=255, required=False,
        help_text='Tylko gdy osoby nie ma w spisie (także wśród nieaktywnych).')
    new_engineer_email = forms.EmailField(label='Nowy montażysta – e-mail', required=False)

    class Meta:
        model = Audiobook
        fields = ('narrator_contact', 'engineer_contact')
        field_classes = {'narrator_contact': ContactChoice, 'engineer_contact': ContactChoice}
        labels = {'narrator_contact': 'Lektor', 'engineer_contact': 'Montaż'}

    def __init__(self, *args, **kwargs):
        from core.audiobook_models import AudioContributor
        super().__init__(*args, **kwargs)
        self.new_people = {}
        for role in self.ROLES:
            field = self.fields[f'{role}_contact']
            current = getattr(self.instance, f'{role}_contact_id', None)
            field.queryset = AudioContributor.objects.filter(Q(is_active=True) | Q(pk=current)).order_by('name', 'pk')
            field.empty_label = 'Nie przypisano'
            field.widget.attrs['data-searchable-select'] = ''

    def clean(self):
        from core.audiobook_models import AudioContributor, contact_key
        data = super().clean()
        for role, genitive in self.ROLES.items():
            name = ' '.join(data.get(f'new_{role}_name', '').split())
            email = (data.get(f'new_{role}_email') or '').strip().lower()
            if email and not name:
                self.add_error(f'new_{role}_name', f'Podaj imię i nazwisko nowego {genitive}.')
            if not name:
                continue
            if data.get(f'{role}_contact'):
                self.add_error(f'new_{role}_name', 'Wybierz osobę ze spisu albo dopisz nową – nie obie naraz.')
                continue
            known = (AudioContributor.objects.filter(email__iexact=email).exists() if email else False) or any(
                contact_key(person.name) == contact_key(name) for person in AudioContributor.objects.only('name'))
            if known:
                self.add_error(f'new_{role}_name', 'Ta osoba jest już w spisie lektorów i montażu. Wybierz ją z listy.')
            else:
                self.new_people[role] = AudioContributor(name=name, email=email)
        return data

    def save(self, commit=True):
        from django.db import transaction
        with transaction.atomic():
            for role, person in self.new_people.items():
                person.full_clean()
                person.save()
                setattr(self.instance, f'{role}_contact', person)
            return super().save(commit)


class AudiobookStatusForm(forms.Form):
    status = forms.ChoiceField(label='Nowy status', choices=Audiobook.Status.choices)
    date = forms.DateField(label='Data zmiany', required=False,
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
        help_text='Kończy bieżący etap i rozpoczyna etap nowego statusu. Puste pole – data nieznana.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['date'].widget.attrs['max'] = timezone.localdate().isoformat()


class AudiobookNotesForm(forms.ModelForm):
    class Meta:
        model = Audiobook
        fields = ('coordinator_notes',)
        widgets = {'coordinator_notes': forms.Textarea(attrs={'rows': 4})}


class AudiobookPublicationForm(forms.ModelForm):
    class Meta:
        model = Audiobook
        fields = ('premiere_date', 'youtube_url', 'hearthis_url')
        widgets = {'premiere_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})}


class AudiobookAssignmentForm(AudiobookProductionForm):
    class Meta(AudiobookProductionForm.Meta):
        fields = ('proofreader',)
        exclude = ()


class AudiobookStageDateForm(forms.Form):
    """Data rozpoczęcia lub zakończenia. Puste pole = data nieznana (dla starszych nagrań)."""
    date = forms.DateField(label='Data', required=False,
                           widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['date'].widget.attrs['max'] = timezone.localdate().isoformat()

    def clean_date(self):
        value = self.cleaned_data['date']
        if value and value > timezone.localdate():
            raise forms.ValidationError('Data nie może być w przyszłości.')
        return value


class AudiobookStageDatesForm(forms.Form):
    """Daty wszystkich etapów (koordynator). Puste pole – data nieznana; trwający etap nie ma zakończenia."""

    def __init__(self, *args, stages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.stages = list(stages)
        today = timezone.localdate().isoformat()
        for stage in self.stages:
            for prefix, value in (('start', stage.started_at), ('end', stage.ended_at)):
                if prefix == 'end' and not stage.is_completed:
                    continue
                self.fields[f'{prefix}_{stage.pk}'] = forms.DateField(required=False, initial=value,
                    label=f"{stage.get_stage_type_display()} – {'rozpoczęcie' if prefix == 'start' else 'zakończenie'}",
                    widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'max': today}))

    def rows(self):
        return [{'stage': stage, 'start': self[f'start_{stage.pk}'],
                 'end': self[f'end_{stage.pk}'] if f'end_{stage.pk}' in self.fields else None} for stage in self.stages]

    def dates(self):
        return {stage.pk: (self.cleaned_data.get(f'start_{stage.pk}'), self.cleaned_data.get(f'end_{stage.pk}'))
                for stage in self.stages}
