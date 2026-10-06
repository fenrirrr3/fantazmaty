from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from authors.admin import SuperuserOnlyAdminMixin
from texts.models import NovelProfile, VocabularyTerm


@admin.register(NovelProfile)
class NovelProfileAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('anthology', 'open_panel', 'genre', 'approved_at')
    search_fields = ('anthology__title__plcontains', 'tags__plcontains', 'genre__plcontains')
    fields = ('anthology', 'open_panel', 'authors', 'tags', 'genre', 'notes', 'approved_at', 'approved_by')
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Rozdziały i edycja powieści')
    def open_panel(self, obj):
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
