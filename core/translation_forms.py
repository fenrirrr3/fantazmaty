from django import forms
from django.urls import reverse
from texts.models import TextTranslation


class TranslationForm(forms.ModelForm):
    class Meta:
        model = TextTranslation
        fields = ('foreign_authors', 'translators', 'original_verifier')
        widgets = {name: forms.SelectMultiple(attrs={'size': 4}) for name in ('foreign_authors', 'translators')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, kind in (('foreign_authors', 'author'), ('translators', 'translator')):
            field = self.fields[name]
            field.label_from_instance = lambda person: person.display_name
            field.help_text = 'Wyszukaj i wybierz osobę. Możesz dodać kilka osób.'
            field.widget.attrs['data-translation-search-url'] = reverse('core:translation_person_suggestions', args=[kind])
            field.widget.attrs['data-search-label'] = str(field.label)
            if self.is_bound:
                values = self.data.getlist(self.add_prefix(name)) if hasattr(self.data, 'getlist') else self.data.get(self.add_prefix(name), [])
                if not isinstance(values, (list, tuple)):
                    values = [values]
            else:
                values = self.initial.get(name, [])
            ids = [str(getattr(value, 'pk', value)) for value in values]
            ids = [value for value in ids if len(value) <= 19 and value.isascii() and value.isdecimal() and 0 < int(value) <= 9_223_372_036_854_775_807]
            # Render only the selected people; suggestions are fetched on demand.
            # On POST the model field still verifies that each supplied ID exists.
            field.queryset = field.queryset.filter(pk__in=ids)
