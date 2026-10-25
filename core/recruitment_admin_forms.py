"""Explicit role editing, using the same roles as recruitment decisions."""
from django import forms
from core.models import Recruitment
from core.selectors.recruitment import record_roles, role_choices


class RecruitmentAdminForm(forms.ModelForm):
    """Zgłoszenie rekrutacyjne w adminie i na stronie (core:recruitment_add / recruitment_edit)."""
    selected_roles = forms.MultipleChoiceField(
        label='Role zgłoszenia', widget=forms.CheckboxSelectMultiple,
        help_text='Wybierz co najmniej jedną rolę. Nowe role otrzymają stan Bez decyzji. '
                  'Decyzje i notatki usuniętych ról zostają w archiwum; ponowne wybranie roli je przywraca.')

    class Meta:
        model = Recruitment
        fields = ('first_name', 'last_name', 'applicant_name', 'email', 'selected_roles', 'submitted_at', 'notified', 'notes')
        widgets = {'notes': forms.Textarea(attrs={'rows': 3}), 'submitted_at': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.mail_fingerprint:
            # Imported messages need not reveal a candidate's real name yet.
            for name in ('first_name', 'last_name', 'email'):
                self.fields[name].required = False
        self.fields['selected_roles'].choices = role_choices()
        self.initial['selected_roles'] = list(record_roles(self.instance.mail_roles)) if self.instance.pk else ['other']

    def save(self, commit=True):
        instance = super().save(commit=False)
        selected = set(self.cleaned_data['selected_roles'])
        instance.mail_roles = [key for key, _ in role_choices() if key in selected]
        if commit:
            instance.save()
            self.save_m2m()
        return instance
