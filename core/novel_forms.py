from django import forms
from django.contrib.auth import get_user_model

from authors.models import Author
from texts.models import Anthology, NovelProfile, Text
from workflow.catalog import workflow_role_choices
from texts.vocabulary import canonicalize
from texts.novels import parse_chapter_numbers


class AuthorChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.display_name


class NovelForm(forms.ModelForm):
    title = forms.CharField(label='Tytuł powieści', max_length=255)
    authors = AuthorChoices(label='Autorzy z bazy', queryset=Author.objects.all(), required=False,
                           widget=forms.SelectMultiple(attrs={'data-filter-options': 'true', 'size': 6}),
                           help_text='Możesz zaznaczyć kilka osób, przytrzymując Ctrl lub Command.')
    new_author_first_name = forms.CharField(label='Nowy autor – imię', max_length=100, required=False)
    new_author_last_name = forms.CharField(label='Nowy autor – nazwisko', max_length=100, required=False)
    new_author_pseudonym = forms.CharField(label='Nowy autor – pseudonim', max_length=100, required=False)
    new_author_email = forms.EmailField(label='Nowy autor – e-mail (opcjonalnie)', required=False)

    class Meta:
        model = NovelProfile
        fields = ('title', 'authors', 'tags', 'genre', 'content_warnings', 'file_url', 'notes')
        help_texts = {'tags': 'Oddziel tagi przecinkami lub nową linią.'}
        widgets = {'tags': forms.Textarea(attrs={'rows': 3, 'data-vocabulary': 'tag'}),
                   'genre': forms.TextInput(attrs={'data-vocabulary': 'genre'}),
                   'content_warnings': forms.Textarea(attrs={'rows': 2}), 'notes': forms.Textarea(attrs={'rows': 4})}

    def clean(self):
        data = super().clean()
        self.new_author = None
        fields = ('first_name', 'last_name', 'pseudonym', 'email')
        values = {name: data.get('new_author_' + name, '') for name in fields}
        if any(values.values()):
            for name in ('first_name', 'last_name'):
                if not values[name]:
                    self.add_error('new_author_' + name, 'Uzupełnij imię i nazwisko nowego autora.')
            if values['first_name'] and values['last_name']:
                author = Author(**{**values, 'email': values['email'] or None})
                try:
                    author.full_clean(exclude=['email'] if not values['email'] else [])
                except forms.ValidationError as exc:
                    self.add_error(None, ' '.join(exc.messages))
                if Author.objects.filter(first_name__iexact=author.first_name,
                                         last_name__iexact=author.last_name,
                                         pseudonym__iexact=author.pseudonym).exists():
                    self.add_error(None, 'Taki autor już istnieje. Wybierz go z listy autorów z bazy.')
                elif author.email and Author.objects.filter(email__iexact=author.email).exists():
                    self.add_error('new_author_email', 'Ten e-mail jest już przypisany do autora w bazie.')
                else:
                    self.new_author = author
        elif not data.get('authors'):
            self.add_error('authors', 'Wybierz autora z bazy lub uzupełnij dane nowego autora poniżej.')
        return data

    def save_authors(self):
        self.save_m2m()
        if self.new_author is not None:
            self.new_author.save()
            self.instance.authors.add(self.new_author)

    def clean_tags(self):
        return canonicalize(self.cleaned_data['tags'], 'tag')

    def clean_genre(self):
        return canonicalize(self.cleaned_data['genre'], 'genre')


class ChapterForm(forms.ModelForm):
    chapter_number = forms.IntegerField(label='Numer rozdziału', min_value=1)

    class Meta:
        model = Text
        fields = ('chapter_number',)


class ChapterRangeForm(forms.Form):
    chapter_numbers = forms.CharField(label='Numery rozdziałów', max_length=4000,
        help_text='Podaj numery lub zakresy po przecinku, np. 1–2, 4–7. Maksymalnie 500 rozdziałów naraz.',
        widget=forms.TextInput(attrs={'placeholder': 'np. 1–2, 4–7'}))

    def clean_chapter_numbers(self):
        return parse_chapter_numbers(self.cleaned_data['chapter_numbers'])


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
