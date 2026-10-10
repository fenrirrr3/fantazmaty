import re

from django import forms
from django.contrib.auth import get_user_model

from authors.models import Author
from texts.models import Anthology, NovelProfile, Text
from workflow.catalog import workflow_role_choices
from texts.vocabulary import canonicalize
from texts.novels import parse_chapter_numbers
from texts.cover_forms import CoverAssignmentForm


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
    length = forms.IntegerField(label='Liczba znaków ze spacjami', min_value=1, max_value=2147483647, required=False,
                               help_text='Pozostaw puste, jeśli długość nie jest jeszcze znana.')

    class Meta:
        model = Text
        fields = ('chapter_number', 'length')


class ChapterRangeForm(forms.Form):
    chapter_numbers = forms.CharField(label='Numery rozdziałów', max_length=4000,
        help_text='Podaj numery lub zakresy po przecinku, np. 1–2, 4–7. Maksymalnie 500 rozdziałów naraz.',
        widget=forms.TextInput(attrs={'placeholder': 'np. 1–2, 4–7'}))
    chapter_lengths = forms.CharField(label='Liczba znaków ze spacjami', required=False, max_length=16000,
        help_text='Dla jednego rozdziału wpisz samą liczbę. Dla kilku podaj numer i długość, np. 1: 12882, 2: 37435 '
                  '(możesz też użyć osobnych linii). Pominięte długości pozostaną nieznane.',
        widget=forms.Textarea(attrs={'rows': 3, 'class': 'chapter-lengths-input', 'placeholder': 'np. 1: 12882, 2: 37435'}))

    def clean_chapter_numbers(self):
        return parse_chapter_numbers(self.cleaned_data['chapter_numbers'])

    def clean(self):
        data = super().clean()
        numbers = data.get('chapter_numbers')
        raw = data.get('chapter_lengths', '').strip()
        data['chapter_lengths'] = {}
        if not numbers or not raw:
            return data

        def positive_integer(value):
            compact = re.sub(r'[ \u00a0\u202f]', '', value.strip())
            if not re.fullmatch(r'[0-9]{1,10}', compact) or not 1 <= int(compact) <= 2147483647:
                raise forms.ValidationError('Numer i liczba znaków muszą być dodatnimi liczbami całkowitymi, nie większymi niż 2147483647.')
            return int(compact)

        try:
            if ':' not in raw:
                if len(numbers) != 1:
                    raise forms.ValidationError('Przy kilku rozdziałach podaj osobną długość z numerem, np. 1: 12882, 2: 37435.')
                data['chapter_lengths'] = {numbers[0]: positive_integer(raw)}
            else:
                lengths = {}
                for item in re.split(r'[,;\r\n]+', raw):
                    if not item.strip():
                        continue
                    if item.count(':') != 1:
                        raise forms.ValidationError('Użyj zapisu numer: liczba znaków, np. 1: 12882.')
                    number, length = (positive_integer(part) for part in item.split(':'))
                    if number not in numbers:
                        raise forms.ValidationError(f'Rozdział {number} nie znajduje się w podanych numerach do dodania.')
                    if number in lengths:
                        raise forms.ValidationError(f'Liczbę znaków rozdziału {number} wpisano więcej niż raz.')
                    lengths[number] = length
                data['chapter_lengths'] = lengths
        except forms.ValidationError as exc:
            self.add_error('chapter_lengths', exc)
        return data


class AssigneeChoices(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        person = getattr(obj, 'person_profile', None)
        return str(person) if person else (obj.get_full_name() or obj.get_username())


class RoleAssigneeSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value:
            option['attrs']['data-assignment-roles'] = ' '.join(self.role_memberships.get(value.value, []))
        return option


class AssignmentForm(forms.Form):
    role = forms.ChoiceField(label='Dopisz do roli', choices=())
    assignee = AssigneeChoices(label='Członek zespołu', queryset=get_user_model().objects.none(),
                             widget=RoleAssigneeSelect(attrs={'data-searchable-person': 'true'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from workflow.availability import eligible_role_users
        self.fields['role'].choices = [('', 'Wybierz rolę')] + list(workflow_role_choices())
        memberships = {}
        for code, _ in workflow_role_choices():
            for pk in eligible_role_users(code).values_list('pk', flat=True):
                memberships.setdefault(pk, []).append(code)
        widget = self.fields['assignee'].widget
        widget.role_memberships = memberships
        widget.attrs['data-assignment-role-select'] = self['role'].auto_id
        users = get_user_model().objects.filter(pk__in=memberships)
        self.fields['assignee'].queryset = users.select_related('person_profile').order_by('last_name', 'first_name', 'pk')

    def clean(self):
        data = super().clean()
        if bool(data.get('role')) != bool(data.get('assignee')):
            raise forms.ValidationError('Wybierz zarówno rolę, jak i wykonawcę z uprawnieniami do tej roli.')
        if data.get('role') and data.get('assignee'):
            from workflow.availability import eligible_role_users
            if not eligible_role_users(data['role']).filter(pk=data['assignee'].pk).exists():
                self.add_error('assignee', 'Ta osoba nie może pełnić wybranej roli.')
        return data


class CoverForm(CoverAssignmentForm):
    class Meta(CoverAssignmentForm.Meta):
        model = Anthology
        fields = (*CoverAssignmentForm.Meta.fields, 'print_status')
        widgets = {'cover_notes': forms.Textarea(attrs={'rows': 2})}
