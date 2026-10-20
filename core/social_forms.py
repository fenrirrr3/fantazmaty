"""Formularze zakładki Social Media (zapis jednego wiersza tabeli)."""
from django import forms

from core.social_models import SocialMediaEntry, SocialMediaQuote, TextSocialMedia


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


class QuoteContentField(forms.CharField):
    """Treść cytatu: końce wierszy z przeglądarki (\\r\\n) liczą się jak jeden znak, jak w polu maxlength."""
    def to_python(self, value):
        return super().to_python(value).replace('\r\n', '\n')


class SocialMediaQuoteForm(forms.ModelForm):
    """Nowy cytat: tylko treść (tekst, autora i datę ustawia widok)."""
    content = QuoteContentField(label='Cytat', max_length=2000, widget=forms.Textarea, error_messages={
        'required': 'Wpisz cytat.', 'max_length': 'Cytat może mieć najwyżej %(limit_value)d znaków.'})

    class Meta:
        model = SocialMediaQuote
        fields = ('content',)


class SocialMediaQuoteStatusForm(forms.ModelForm):
    """Status i data wrzucenia cytatu (osoby od Social Media)."""
    posted_on = forms.DateField(label='Data wrzucenia', widget=forms.DateInput(attrs={'type': 'date'}),
                                input_formats=['%Y-%m-%d', '%d.%m.%Y'],
                                error_messages={'invalid': 'Podaj prawidłową datę.', 'required': 'Podaj datę wrzucenia.'})

    class Meta:
        model = SocialMediaQuote
        fields = ('status', 'posted_on')
