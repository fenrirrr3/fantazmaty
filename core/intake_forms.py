from core.translation_scope import ordinary
from django import forms
from core.models import Recruitment
from texts.models import Extract, Anthology
from django.urls import reverse
from core.review_submission_forms import ReviewAdminForm


class ExtractForm(forms.ModelForm):
    class Meta:
        model = Extract
        fields = ('author', 'full_name', 'email', 'phone_number', 'title', 'submission_dates', 'recruitment', 'accepted_titles', 'rejected_titles')
        widgets = {name: forms.Textarea(attrs={'rows': 4}) for name in ('title', 'submission_dates', 'accepted_titles', 'rejected_titles')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['author'].label_from_instance = lambda author: author.display_name
        if self.instance.pk and self.instance.author.pseudonym.strip():
            self.initial['full_name'] = self.instance.author.display_name
            self.fields['full_name'].disabled = True
        self.fields['submission_dates'].required = True
        self.fields['full_name'].required = False
        self.fields['email'].required = False
        self.fields['full_name'].help_text = 'Puste pole zostanie uzupełnione danymi wybranego autora.'
        self.fields['email'].help_text = self.fields['full_name'].help_text

    def clean(self):
        data = super().clean()
        if data.get('author'):
            if self.fields['full_name'].disabled:
                data['full_name'] = self.instance.full_name if data['author'].pk == self.instance.author_id else ''
            data['full_name'] = data.get('full_name') or str(data['author'])
            data['email'] = data.get('email') or data['author'].email
            if not data['email']:
                self.add_error('email', 'Uzupełnij adres e-mail autora historycznego.')
        return data


class RecruitmentForm(forms.ModelForm):
    class Meta:
        model = Recruitment
        fields = ('first_name', 'last_name', 'email', 'department', 'submitted_at', 'status', 'notified', 'notes', 'unofficial_notes')
        widgets = {'notes': forms.Textarea(attrs={'rows': 3}), 'unofficial_notes': forms.Textarea(attrs={'rows': 3}), 'submitted_at': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})}


class SingleReviewForm(ReviewAdminForm):
    newsletter_premieres = forms.BooleanField(label="Zgoda na newsletter o premierach", required=False)
    newsletter_recruitment = forms.BooleanField(label="Zgoda na newsletter o naborach", required=False)

    """Te same podpisane ostrzeżenia co w istniejącym formularzu admina."""
    class Meta(ReviewAdminForm.Meta):
        fields = ('anthology', 'author', 'author_first_name', 'author_last_name', 'author_pseudonym',
                  'title', 'genre', 'content_warnings', 'length', 'email', 'phone_number',
                  'newsletter_premieres', 'newsletter_recruitment', 'author_message')
        labels = {
            'author_first_name': 'Imię', 'author_last_name': 'Nazwisko',
            'author_pseudonym': 'Pseudonim', 'title': 'Tytuł opowiadania',
            'genre': 'Gatunek', 'content_warnings': 'Ostrzeżenia o treści',
            'length': 'Liczba znaków ze spacjami', 'email': 'Adres e-mail',
            'phone_number': 'Numer telefonu', 'author_message': 'Wiadomość do redakcji',
        }
        widgets = {'author_message': forms.Textarea(attrs={'rows': 4}), 'content_warnings': forms.Textarea(attrs={'rows': 2, 'class': 'short-textarea'}),
                   'author': forms.Select()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # These confirmations concern linking an existing text in Django admin;
        # the single-submission form has no such operation.
        self.fields.pop('confirm_existing_text', None)
        self.fields.pop('confirm_source_mismatch', None)
        self.fields['newsletter_premieres'].label = 'Premierach'
        self.fields['newsletter_recruitment'].label = 'Naborach'
        self.fields['anthology'].queryset = ordinary(Anthology.objects).filter(is_novel=False).filter(status=Anthology.Status.IN_PREPARATION).order_by('title', 'pk')
        self.fields['author'].widget.attrs['data-author-search-url'] = reverse('core:author_suggestions')
        from authors.models import Author
        author_id = self.data.get('author') if self.is_bound else self.initial.get('author', self.instance.author_id)
        self.fields['author'].queryset = Author.objects.filter(pk=author_id) if str(author_id or '').isascii() and str(author_id or '').isdecimal() and len(str(author_id)) < 19 else Author.objects.none()
        # Without JavaScript the form still accepts manually entered author details.

        self.fields['author'].label_from_instance = lambda author: author.display_name


    def clean(self):
        data = super().clean()
        if data.get("author") and not data.get("phone_number"):
            from core.author_contact import stored_author_phone
            data["phone_number"] = stored_author_phone(data["author"])
        return data
