"""Forms and optimistic locking for the illustration workspace."""
from django import forms
from django.core.validators import URLValidator
from django.db.models import Q

from core.edit_versions import object_token, object_token_matches
from core.permissions import is_coordinator
from .models import Illustration, Illustrator


def edit_token(user, illustration):
    return object_token(user, illustration, 'illustration-edit')


def token_matches(token, user, illustration):
    return object_token_matches(token, user, illustration, 'illustration-edit')


class IllustrationEditForm(forms.ModelForm):
    version = forms.CharField(widget=forms.HiddenInput)


class AssignmentModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'illustrators' in self.fields:
            selected = self.instance.illustrators.values_list('pk', flat=True) if self.instance.pk else []
            self.fields['illustrators'].queryset = Illustrator.objects.filter(Q(is_active=True) | Q(pk__in=selected))
        if 'text' in self.fields:
            from .services import illustration_texts_queryset
            from texts.models import Text
            allowed = illustration_texts_queryset().values('pk')
            self.fields['text'].queryset = Text.objects.filter(Q(pk__in=allowed) | Q(pk=self.instance.text_id))
            if self.instance.pk:
                # Ilustracja z zasługami należy do jednego tekstu; strona nigdy jej nie przenosi.
                self.fields['text'].disabled = True
                self.fields['text'].help_text = 'Ilustracji nie przenosi się do innego tekstu.'

    class Meta:
        model = Illustration
        fields = ('illustrators', 'status')

    def clean_story_url(self):
        value = self.cleaned_data.get('story_url')
        if value:
            URLValidator(schemes=['https', 'http'])(value)
        return value

    def clean(self):
        cleaned = super().clean()
        if 'illustrators' in self.fields:
            self.instance._selected_illustrator_ids = {p.pk for p in cleaned.get('illustrators', [])}
        return cleaned

    def _save_m2m(self):
        super()._save_m2m()
        if hasattr(self.instance, '_selected_illustrator_ids'):
            del self.instance._selected_illustrator_ids


class ContactCheckboxes(forms.CheckboxSelectMultiple):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if getattr(value, 'instance', None):
            option['attrs']['data-email'] = value.instance.email or ''
        return option


class AssignmentForm(AssignmentModelForm, IllustrationEditForm):
    """Przypisanie ilustratorów ze spisu; osobę spoza spisu dopisuje się do spisu przy zapisie."""
    new_illustrator_name = forms.CharField(label='Nowy ilustrator – imię i nazwisko', max_length=200, required=False,
        help_text='Tylko gdy osoby nie ma w spisie (także wśród nieaktywnych).')
    new_illustrator_email = forms.EmailField(label='Nowy ilustrator – e-mail', required=False)

    class Meta(AssignmentModelForm.Meta):
        widgets = {'illustrators': ContactCheckboxes(attrs={'class': 'illustrator-choices'})}

    def __init__(self, *args, can_assign=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.new_artist = None
        if can_assign:
            from .contact_forms import duplicate_display_names, contact_label
            duplicates = duplicate_display_names()
            selected = self.instance.illustrators.values_list('pk', flat=True) if self.instance.pk else []
            self.fields['illustrators'].label_from_instance = lambda person: contact_label(person, duplicates)
            self.fields['illustrators'].queryset = Illustrator.objects.filter(
                Q(pk__in=selected) | Q(is_active=True)
            ).annotate(artist_name=Illustrator.display_name_expression()).order_by('artist_name', 'pk')
        else:
            for name in ('illustrators', 'new_illustrator_name', 'new_illustrator_email'):
                del self.fields[name]

    def clean(self):
        cleaned = super().clean()
        name = ' '.join(cleaned.get('new_illustrator_name', '').split())
        email = (cleaned.get('new_illustrator_email') or '').strip()
        if email and not name:
            self.add_error('new_illustrator_name', 'Podaj imię i nazwisko nowego ilustratora.')
        if name:
            from texts.cover_forms import matching_artists
            first, _, last = name.rpartition(' ') if ' ' in name else (name, '', '')
            limit = Illustrator._meta.get_field('first_name').max_length
            if len(first) > limit or len(last) > Illustrator._meta.get_field('last_name').max_length:
                self.add_error('new_illustrator_name', f'Imię i nazwisko mogą mieć najwyżej po {limit} znaków.')
                return cleaned
            existing = Illustrator.objects.filter(email__iexact=email).first() if email else None
            if existing or matching_artists(first, last):
                self.add_error('new_illustrator_name', 'Ta osoba jest już w spisie ilustratorów. Wybierz ją z wyszukiwarki.')
            else:
                self.new_artist = Illustrator(first_name=first, last_name=last, email=email or None)
        if self.new_artist is not None and 'illustrators' in self.fields:
            # Walidacja modelu widzi przypisanie (wpis powstanie przy zapisie).
            self.instance._selected_illustrator_ids = {p.pk for p in cleaned.get('illustrators', [])} | {0}
        return cleaned

    def save(self, commit=True):
        if self.new_artist is not None:
            from django.db import transaction
            with transaction.atomic():
                self.new_artist.full_clean()
                self.new_artist.save()
                artists = [*self.cleaned_data.get('illustrators', []), self.new_artist]
                self.cleaned_data['illustrators'] = artists
                self.instance._selected_illustrator_ids = {p.pk for p in artists}
                return super().save(commit)
        return super().save(commit)


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
