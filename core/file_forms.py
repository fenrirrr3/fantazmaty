from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.forms.models import construct_instance
from texts.models import Text, Review


class TextFileForm(forms.ModelForm):
    file_url = forms.URLField(label='Link do folderu Dropbox', required=False, max_length=1000, validators=[URLValidator(schemes=['https', 'http'])])
    class Meta:
        model = Text
        fields = ('file_url',)

    def _post_clean(self):
        # This is a partial update. Legacy records may lack unrelated intake
        # fields; model.clean() must not reject a change of their folder link.
        try:
            self.instance = construct_instance(self, self.instance, self._meta.fields)
            if 'file_url' in self.cleaned_data:
                field = self.instance._meta.get_field('file_url')
                self.instance.file_url = field.clean(self.instance.file_url, self.instance)
        except ValidationError as error:
            self._update_errors(error)


class ReviewFileForm(TextFileForm):
    class Meta(TextFileForm.Meta):
        model = Review
