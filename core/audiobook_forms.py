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
        widgets = {name: forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}) for name in (
            'recording_started_at', 'proofreading_started_at', 'corrections_started_at',
            'editing_started_at', 'awaiting_publication_started_at', 'premiere_date')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Preserve a former member in existing assignments, but do not offer
        # inactive accounts for new assignments.
        ids = eligible_proofreaders().values('pk')
        self.fields['proofreader'].queryset = get_user_model().objects.filter(
            Q(pk__in=ids) | Q(pk=self.instance.proofreader_id)
        ).select_related('person_profile').order_by('last_name', 'first_name', 'pk')

    def clean(self):
        from core.audiobook_models import contact_key
        data = super().clean()
        for role in ('narrator', 'engineer'):
            if f'{role}_name' not in self.fields or f'{role}_contact' in self.fields:
                continue
            contact = getattr(self.instance, f'{role}_contact', None)
            name, email = data.get(f'{role}_name', ''), (data.get(f'{role}_email') or '').strip().lower()
            if contact and (contact_key(contact.name) != contact_key(name) or (email and email != contact.email)):
                # Another person was typed: link (or create) that person's own profile.
                setattr(self.instance, f'{role}_contact', None)
        return data


class AudiobookPeopleForm(forms.ModelForm):
    class Meta:
        model = Audiobook
        fields = ('narrator_name', 'narrator_email', 'narrator_contact', 'engineer_name', 'engineer_email', 'engineer_contact')
        widgets = {'narrator_contact': forms.HiddenInput(), 'engineer_contact': forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for role in ('narrator', 'engineer'):
            for field in ('name', 'email'):
                self.fields[f'{role}_{field}'].widget.attrs.update({'data-contact-kind': 'audio', 'data-contact-group': role,
                    'data-contact-field': field, 'autocomplete': 'off'})
            self.fields[f'{role}_contact'].widget.attrs.update({'data-contact-group': role, 'data-contact-field': 'id'})
            self.fields[f'{role}_name'].help_text = 'Zacznij pisać, aby wybrać osobę z bazy. Nowa osoba otrzyma własny profil.'

    def clean(self):
        from core.audiobook_models import contact_key
        data = super().clean()
        for role in ('narrator', 'engineer'):
            contact = data.get(f'{role}_contact')
            name = data.get(f'{role}_name', '')
            if contact and contact_key(contact.name) != contact_key(name):
                # The typed name no longer matches the chosen profile: look it up again.
                data[f'{role}_contact'] = None
        return data


class AudiobookPublicationForm(forms.ModelForm):
    class Meta:
        model = Audiobook
        fields = ('premiere_date', 'youtube_url', 'hearthis_url')
        widgets = {'premiere_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})}


class AudiobookAssignmentForm(AudiobookProductionForm):
    class Meta(AudiobookProductionForm.Meta):
        fields = ('proofreader',)
        exclude = ()


class AudiobookStageForm(forms.Form):
    stage_type = forms.ChoiceField(label='Etap', choices=())
    started_at = forms.DateField(label='Data rozpoczęcia', initial=timezone.localdate,
        help_text='Przy etapie Opublikowane to data publikacji; etap nie wymaga zakończenia.',
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    def __init__(self, *args, allowed=None, **kwargs):
        super().__init__(*args, **kwargs)
        from core.audiobook_services import STAGE_ORDER
        labels = dict(Audiobook.Status.choices)
        kinds = STAGE_ORDER if allowed is None else allowed
        self.fields['stage_type'].choices = [(kind, labels[kind]) for kind in kinds]

    def clean_started_at(self):
        value = self.cleaned_data['started_at']
        if value > timezone.localdate():
            raise forms.ValidationError('Data rozpoczęcia nie może być w przyszłości.')
        return value
