from django import forms
from texts.models import TextTranslation


class TranslationForm(forms.ModelForm):
    class Meta:
        model = TextTranslation
        fields = ('foreign_authors', 'translators')
        widgets = {name: forms.SelectMultiple(attrs={'size': 4}) for name in ('foreign_authors', 'translators')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['translators'].help_text = 'Wybierz osobę z oddzielnego spisu tłumaczy.'
        self.fields['foreign_authors'].help_text = 'Wybierz osobę ze spisu autorów zagranicznych.'
