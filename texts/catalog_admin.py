from django.contrib import admin
from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.html import format_html

from authors.admin import SuperuserOnlyAdminMixin
from texts.models import NovelProfile, VocabularyTerm
from texts.novels import edit_token, locked_book, sync_metadata
from texts.vocabulary import canonicalize


class NovelAdminForm(forms.ModelForm):
    title = forms.CharField(label='Tytuł powieści', max_length=255)
    novel_token = forms.CharField(widget=forms.HiddenInput)
    novel_user = None

    class Meta:
        model = NovelProfile
        fields = ('title', 'authors', 'tags', 'genre', 'content_warnings', 'file_url', 'notes')
        help_texts = {'tags': 'Oddziel tagi przecinkami lub nową linią.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'authors' in self.fields:
            self.fields['authors'].required = True
        if self.instance.pk:
            self.initial['title'] = self.instance.anthology.title
            if self.novel_user:
                self.initial['novel_token'] = edit_token(self.instance.anthology, self.novel_user)

    def clean_tags(self):
        return canonicalize(self.cleaned_data['tags'], 'tag')

    def clean_genre(self):
        return canonicalize(self.cleaned_data['genre'], 'genre')

    def clean(self):
        data = super().clean()
        if self.instance.pk and not self.instance.anthology.is_novel:
            self.add_error(None, 'Publikacja nie jest oznaczona jako powieść. Włącz „Powieść” w antologii, aby edytować zachowane dane.')
        if not self.errors and self.instance.pk:
            try:
                # Django admin keeps an outer transaction open through save_related.
                with locked_book(self.instance.anthology_id, self.novel_user, data['novel_token']):
                    pass
            except ValidationError as exc:
                self.add_error(None, ' '.join(exc.messages))
        return data


@admin.register(NovelProfile)
class NovelProfileAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = NovelAdminForm
    list_display = ('anthology', 'open_panel', 'genre')
    search_fields = ('anthology__title__plcontains', 'tags__plcontains', 'genre__plcontains')
    fields = ('anthology', 'open_panel', 'title', 'authors', 'tags', 'genre', 'content_warnings', 'file_url', 'notes', 'novel_token')
    readonly_fields = ('anthology', 'open_panel')
    autocomplete_fields = ('authors',)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.novel_user = request.user
        return form

    def save_model(self, request, obj, form, change):
        book = obj.anthology
        book.title = form.cleaned_data['title']
        book.save(update_fields=['title'])
        obj.tags = canonicalize(obj.tags, 'tag', register=True)
        obj.genre = canonicalize(obj.genre, 'genre', register=True)
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        sync_metadata(form.instance.anthology, form.instance)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Rozdziały powieści')
    def open_panel(self, obj):
        if not obj.anthology.is_novel:
            return format_html('<a href="{}">Zachowany profil – włącz „Powieść” w antologii</a>', reverse('admin:texts_anthology_change', args=[obj.anthology_id]))
        return format_html('<a href="{}">Otwórz panel powieści</a>', reverse('core:novel_detail', args=[obj.anthology_id]))


@admin.register(VocabularyTerm)
class VocabularyTermAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'kind', 'canonical', 'merge_link')
    list_filter = ('kind',)
    search_fields = ('name__plcontains',)
    fields = ('name', 'kind', 'canonical', 'merge_link')
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Scalenie / zmiana nazwy')
    def merge_link(self, obj):
        return format_html('<a href="{}">Podgląd i scalenie w słowniku</a>', reverse('core:vocabulary_merge', args=[obj.pk]))
