from django import forms
from django.contrib.auth import get_user_model

from authors.models import Author
from texts.models import Anthology, NovelProfile, Text
from workflow.catalog import workflow_role_choices
from texts.vocabulary import canonicalize


class AuthorChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.display_name


class NovelForm(forms.ModelForm):
    title = forms.CharField(label='Tytuł powieści', max_length=255)
    authors = AuthorChoices(label='Autorzy', queryset=Author.objects.all(), required=True,
                           widget=forms.SelectMultiple(attrs={'data-filter-options': 'true', 'size': 6}),
                           help_text='Możesz zaznaczyć kilka osób, przytrzymując Ctrl lub Command.')

    class Meta:
        model = NovelProfile
        fields = ('title', 'authors', 'tags', 'genre', 'content_warnings', 'file_url', 'notes')
        widgets = {'tags': forms.Textarea(attrs={'rows': 3, 'data-vocabulary': 'tag'}),
                   'genre': forms.TextInput(attrs={'data-vocabulary': 'genre'}),
                   'content_warnings': forms.Textarea(attrs={'rows': 2}), 'notes': forms.Textarea(attrs={'rows': 4})}

    def clean_tags(self):
        return canonicalize(self.cleaned_data['tags'], 'tag')

    def clean_genre(self):
        return canonicalize(self.cleaned_data['genre'], 'genre')


class ChapterForm(forms.ModelForm):
    chapter_number = forms.IntegerField(label='Numer rozdziału', min_value=1)

    class Meta:
        model = Text
        fields = ('chapter_number', 'title', 'length', 'file_url', 'content_warnings')
        widgets = {'content_warnings': forms.Textarea(attrs={'rows': 2})}


class AssigneeChoices(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        person = getattr(obj, 'person_profile', None)
        return str(person) if person else (obj.get_full_name() or obj.get_username())


class AssignmentForm(forms.Form):
    role = forms.ChoiceField(label='Rola / etap', choices=(), required=False)
    assignee = AssigneeChoices(label='Wykonawca', queryset=get_user_model().objects.none(), required=False,
                             widget=forms.Select(attrs={'data-searchable-person': 'true'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from workflow.availability import eligible_role_users
        self.fields['role'].choices = [('', 'Wybierz rolę')] + list(workflow_role_choices())
        role = self.data.get(self.add_prefix('role')) if self.is_bound else None
        users = eligible_role_users(role) if role in dict(workflow_role_choices()) else get_user_model().objects.filter(is_active=True)
        self.fields['assignee'].queryset = users.select_related('person_profile').order_by('last_name', 'first_name', 'pk')

    def clean(self):
        data = super().clean()
        if bool(data.get('role')) != bool(data.get('assignee')):
            raise forms.ValidationError('Wybierz zarówno rolę, jak i wykonawcę z uprawnieniami do tej roli.')
        return data


AssignmentForms = forms.formset_factory(AssignmentForm, extra=4, max_num=10, validate_max=True, absolute_max=10)


class CoverForm(forms.ModelForm):
    class Meta:
        model = Anthology
        fields = ('cover_status', 'cover_author', 'cover_notes', 'print_status', 'has_illustrations')
        widgets = {'cover_notes': forms.Textarea(attrs={'rows': 2})}
