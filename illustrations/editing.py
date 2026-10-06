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


class AssignmentForm(IllustrationEditForm):
    class Meta:
        model = Illustration
        fields = ('illustrator', 'manual_illustrator_name', 'manual_illustrator_email', 'status')

    def __init__(self, *args, can_assign=False, **kwargs):
        super().__init__(*args, **kwargs)
        if can_assign:
            self.fields['illustrator'].queryset = Illustrator.objects.filter(
                Q(pk=self.instance.illustrator_id)
                | Q(is_active=True)
            ).order_by('last_name', 'first_name', 'pk')
        else:
            for name in ('illustrator', 'manual_illustrator_name', 'manual_illustrator_email'):
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
