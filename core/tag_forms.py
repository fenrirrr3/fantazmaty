import re
from django import forms
from texts.models import Text


class TextTagsForm(forms.ModelForm):
    class Meta:
        model = Text
        fields = ('tags',)
        widgets = {'tags': forms.Textarea(attrs={'rows': 4, 'placeholder': 'np. magia, podróż, przyjaźń'})}
        help_texts = {'tags': 'Oddziel tagi przecinkami lub nową linią. Zapisujesz całą listę tagów tekstu.'}

    def clean_tags(self):
        seen, result = set(), []
        for item in re.split(r'[,\r\n]+', self.cleaned_data['tags']):
            tag = ' '.join(item.split())
            if tag and tag.casefold() not in seen:
                seen.add(tag.casefold())
                result.append(tag)
        return ', '.join(result)
