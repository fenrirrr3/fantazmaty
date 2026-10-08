"""Explicit role editing, using the same roles as recruitment decisions."""
from django import forms
from core.intake_forms import RecruitmentForm
from core.models import Recruitment
from core.selectors.recruitment import record_roles, role_choices


class RecruitmentAdminForm(RecruitmentForm):
    selected_roles = forms.MultipleChoiceField(
        label='Role zgłoszenia', widget=forms.CheckboxSelectMultiple,
        help_text='Wybierz co najmniej jedną rolę. Nowe role otrzymają stan Bez decyzji. '
                  'Decyzje i notatki usuniętych ról zostają w archiwum; ponowne wybranie roli je przywraca.')

    class Meta(RecruitmentForm.Meta):
        fields = tuple('selected_roles' if name == 'department' else name for name in RecruitmentForm.Meta.fields)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['selected_roles'].choices = role_choices()
        self.initial['selected_roles'] = list(record_roles(self.instance.mail_roles, self.instance.department)) if self.instance.pk else ['other']

    def save(self, commit=True):
        instance = super().save(commit=False)
        selected = set(self.cleaned_data['selected_roles'])
        instance.mail_roles = [key for key, _ in role_choices() if key in selected]
        instance.department = instance.mail_roles[0] if len(instance.mail_roles) == 1 and instance.mail_roles[0] in Recruitment.Department.values else Recruitment.Department.OTHER
        if commit:
            instance.save()
            self.save_m2m()
        return instance
