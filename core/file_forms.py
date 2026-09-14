from django import forms
from django.core.validators import URLValidator
from texts.models import Text


class TextFileForm(forms.ModelForm):
    file_url = forms.URLField(label='Link do aktualnego pliku lub folderu', required=False, max_length=1000, validators=[URLValidator(schemes=['https', 'http'])])
    class Meta:
        model = Text
        fields = ('file_url',)
