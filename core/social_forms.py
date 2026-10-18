"""Formularze zakładki Social Media (zapis jednego wiersza tabeli)."""
from django import forms

from core.social_models import SocialMediaEntry, TextSocialMedia


class SocialMediaFieldsForm(forms.ModelForm):
    # assume_scheme: adres bez protokołu („fantazmaty.pl/…”) dostaje https://.
    url = forms.URLField(label='Link', required=False, max_length=1000, assume_scheme='https',
                         error_messages={'invalid': 'Podaj prawidłowy link zaczynający się od http:// lub https://.'})
    posted_on = forms.DateField(label='Data wrzucenia', required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                                input_formats=['%Y-%m-%d', '%d.%m.%Y'],
                                error_messages={'invalid': 'Podaj prawidłową datę.'})


class TextSocialMediaForm(SocialMediaFieldsForm):
    class Meta:
        model = TextSocialMedia
        fields = ('status', 'posted_on', 'url')


class SocialMediaEntryForm(SocialMediaFieldsForm):
    class Meta:
        model = SocialMediaEntry
        fields = ('description', 'status', 'posted_on', 'url')
        error_messages = {'description': {'required': 'Wpisz krótki opis.',
                                          'max_length': 'Krótki opis może mieć najwyżej %(limit_value)d znaków.'}}

    def clean_description(self):
        return ' '.join(self.cleaned_data['description'].split())
