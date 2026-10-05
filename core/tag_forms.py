import re
from django import forms
from texts.models import Text


class TextTagsForm(forms.ModelForm):
    class Meta:
        model = Text
        fields = ('tags', 'genre')
        widgets = {'tags': forms.Textarea(attrs={'rows': 3, 'placeholder': 'np. magia, podróż, przyjaźń'}),
                   'genre': forms.Textarea(attrs={'rows': 2, 'placeholder': 'np. fantasy, groza'})}
        help_texts = {'tags': 'Oddziel tagi przecinkami lub nową linią. Zapisujesz całą listę tagów tekstu.',
                      'genre': 'Gatunki oddziel przecinkami. Maksymalnie 100 znaków.'}

    def clean_tags(self):
        seen, result = set(), []
        for item in re.split(r'[,\r\n]+', self.cleaned_data['tags']):
            tag = ' '.join(item.split())
            if tag and tag.casefold() not in seen:
                seen.add(tag.casefold())
                result.append(tag)
        return ', '.join(result)
