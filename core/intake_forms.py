from django import forms
from core.models import Recruitment
from texts.models import Extract, Anthology
from django.urls import reverse
from texts.admin import ReviewAdminForm


class ExtractForm(forms.ModelForm):
    class Meta:
        model = Extract
        fields = ('author', 'full_name', 'email', 'phone_number', 'title', 'submission_dates', 'recruitment', 'accepted_titles', 'rejected_titles')
        widgets = {name: forms.Textarea(attrs={'rows': 4}) for name in ('title', 'submission_dates', 'accepted_titles', 'rejected_titles')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['submission_dates'].required = True
        self.fields['full_name'].required = False
        self.fields['email'].required = False
        self.fields['full_name'].help_text = 'Puste pole zostanie uzupełnione danymi wybranego autora.'
        self.fields['email'].help_text = self.fields['full_name'].help_text

    def clean(self):
        data = super().clean()
        if data.get('author'):
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
    """Te same podpisane ostrzeżenia co w istniejącym formularzu admina."""
    class Meta(ReviewAdminForm.Meta):
        fields = ('author', 'author_first_name', 'author_last_name', 'email', 'phone_number',
                  'title', 'genre', 'length', 'content_warnings', 'anthology')
        widgets = {'content_warnings': forms.Textarea(attrs={'rows': 2, 'class': 'short-textarea'}),
                   'author': forms.Select()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['anthology'].queryset = Anthology.objects.filter(status=Anthology.Status.IN_PREPARATION).order_by('title', 'pk')
        self.fields['author'].widget.attrs['data-author-search-url'] = reverse('core:author_suggestions')
        from authors.models import Author
        author_id = self.data.get('author') if self.is_bound else self.initial.get('author', self.instance.author_id)
        self.fields['author'].queryset = Author.objects.filter(pk=author_id) if str(author_id or '').isascii() and str(author_id or '').isdecimal() and len(str(author_id)) < 19 else Author.objects.none()
        # Without JavaScript the form still accepts manually entered author details.

        self.fields['author'].label_from_instance = lambda author: f'{author} ({author.pseudonym})' if author.pseudonym else str(author)


    def clean(self):
        data = super().clean()
        if data.get("author") and not data.get("phone_number"):
            from core.author_contact import stored_author_phone
            data["phone_number"] = stored_author_phone(data["author"])
        return data
