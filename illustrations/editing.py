"""Forms and optimistic locking for the illustration workspace."""
from django import forms
from django.core import signing
from django.core.validators import URLValidator
from django.db.models import Q

from core.edit_versions import version_of
from core.permissions import is_coordinator
from .models import Illustration, Illustrator


def can_edit_illustration(user, illustration):
    return is_coordinator(user)


def edit_token(user, illustration):
    return signing.dumps([user.pk, illustration.pk, version_of(illustration)], salt='illustration-edit')


def token_matches(token, user, illustration):
    try:
        return signing.loads(token, salt='illustration-edit', max_age=86400) == [
            user.pk, illustration.pk, version_of(illustration)]
    except signing.BadSignature:
        return False


class IllustrationEditForm(forms.ModelForm):
    version = forms.CharField(widget=forms.HiddenInput)


class AssignmentModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'text' in self.fields:
            from .services import illustration_texts_queryset
            from texts.models import Text
            allowed = illustration_texts_queryset().values('pk')
            self.fields['text'].queryset = Text.objects.filter(Q(pk__in=allowed) | Q(pk=self.instance.text_id))

    class Meta:
        model = Illustration
        fields = ('illustrators', 'manual_illustrator_name', 'manual_illustrator_email', 'status')

    def clean(self):
        cleaned = super().clean()
        if 'illustrators' in self.fields:
            self.instance._selected_illustrator_ids = {p.pk for p in cleaned.get('illustrators', [])}
        return cleaned

    def _save_m2m(self):
        super()._save_m2m()
        if hasattr(self.instance, '_selected_illustrator_ids'):
            del self.instance._selected_illustrator_ids


class AssignmentForm(AssignmentModelForm, IllustrationEditForm):
    class Meta(AssignmentModelForm.Meta):
        widgets = {'illustrators': forms.CheckboxSelectMultiple(attrs={'class': 'illustrator-choices'})}

    def __init__(self, *args, can_assign=False, **kwargs):
        super().__init__(*args, **kwargs)
        if can_assign:
            selected = self.instance.illustrators.values_list('pk', flat=True) if self.instance.pk else []
            self.fields['illustrators'].label_from_instance = lambda person: person.display_name
            self.fields['illustrators'].queryset = Illustrator.objects.filter(
                Q(pk__in=selected) | Q(is_active=True)
            ).annotate(artist_name=Illustrator.display_name_expression()).order_by('artist_name', 'pk')
        else:
            for name in ('illustrators', 'manual_illustrator_name', 'manual_illustrator_email'):
                del self.fields[name]


class StoryLinkForm(IllustrationEditForm):
    story_url = forms.URLField(label='Link do opowiadania', required=False, max_length=500,
        validators=[URLValidator(schemes=['https', 'http'])],
        widget=forms.URLInput(attrs={'placeholder': 'https://...', 'autocomplete': 'off'}))

    class Meta:
        model = Illustration
        fields = ('story_url',)


class ExcerptForm(IllustrationEditForm):
    class Meta:
        model = Illustration
        fields = ('illustrated_excerpt',)
        widgets = {'illustrated_excerpt': forms.Textarea(attrs={
            'rows': 4, 'class': 'illustration-excerpt-input',
            'placeholder': 'Wklej fragment lub wskazówkę dla ilustratora…',
        })}


class CoordinatorNotesForm(IllustrationEditForm):
    class Meta:
        model = Illustration
        fields = ('coordinator_notes',)
        widgets = {'coordinator_notes': forms.Textarea(attrs={'rows': 4})}


FORMS = {'assignment': AssignmentForm, 'link': StoryLinkForm, 'excerpt': ExcerptForm,
         'coordinator_notes': CoordinatorNotesForm}


def edit_forms(user, illustration, *, action=None, data=None):
    initial = {'version': edit_token(user, illustration)}
    return {name: form(data=data if name == action else None, instance=illustration,
                      initial=initial, **({'can_assign': is_coordinator(user)} if name == 'assignment' else {}))
            for name, form in FORMS.items() if name != 'coordinator_notes' or is_coordinator(user)}
