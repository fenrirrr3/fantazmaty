from django import forms
from django.urls import reverse
from texts.models import TextTranslation
from core.filtering import positive_id


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
            ids = [pk for value in values if (pk := positive_id(str(getattr(value, 'pk', value))))]
            # Render only the selected people; suggestions are fetched on demand.
            # On POST the model field still verifies that each supplied ID exists.
            field.queryset = field.queryset.filter(pk__in=ids)
