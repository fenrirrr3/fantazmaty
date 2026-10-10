from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from core.audiobook_models import Audiobook


def eligible_proofreaders():
    return get_user_model().objects.filter(is_active=True, person_profile__is_active=True,
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
