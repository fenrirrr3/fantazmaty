from django import forms
from texts.models import TextTranslation


class TranslationForm(forms.ModelForm):
    class Meta:
        model = TextTranslation
        fields = ('translators',)
        widgets = {'translators': forms.SelectMultiple(attrs={'size': 6})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['translators'].help_text = 'Wybierz tłumaczy ze spisu autorów. Można wskazać więcej niż jedną osobę.'
