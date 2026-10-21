"""Panel administracyjny strony WWW: działy ekipy z podgrupami, ustawienia strony i katalog szukajki."""
from django import forms
from django.contrib import admin
from django.db import models
from django.urls import reverse
from django.utils.html import format_html

from authors.admin import SuperuserOnlyAdminMixin
from core.site_models import (SiteCatalogAnthology, SiteCatalogStory, SiteTeamGroup, SiteTeamSection,
                              SiteTeamSettings)


class SiteTeamGroupInline(SuperuserOnlyAdminMixin, admin.StackedInline):
    model = SiteTeamGroup
    extra = 0
    fields = ('title', 'position', 'source', 'roles', 'manual_names')
    filter_horizontal = ('roles',)


@admin.register(SiteTeamSection)
class SiteTeamSectionAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('title', 'slug', 'position', 'is_enabled', 'show_count', 'in_navigation', 'group_summary')
    list_editable = ('position', 'is_enabled')
    ordering = ('position', 'pk')
    fields = ('title', 'slug', 'position', 'is_enabled', 'show_count', 'in_navigation', 'navigation_label',
              'coordinator_roles', 'manual_coordinators', 'note', 'preview_link')
    readonly_fields = ('preview_link',)
    filter_horizontal = ('coordinator_roles',)
    inlines = (SiteTeamGroupInline,)
    save_on_top = True

    @admin.display(description='podgrupy')
    def group_summary(self, obj):
        return ', '.join(f'{group.title or "lista"} ({group.get_source_display()})' for group in obj.groups.all()) or '–'

    @admin.display(description='podgląd')
    def preview_link(self, obj):
        if not obj.pk:
            return '–'
        return format_html('<a href="{}#podglad-{}">Zakładka System → Ekipa WWW</a>', reverse('core:site_www'), obj.slug)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('groups')


@admin.register(SiteTeamSettings)
class SiteTeamSettingsAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    fields = ('lead_editor', 'site_person', 'last_fetched_at', 'last_fetch_version',
              'catalog_page_url', 'catalog_fetched_at', 'catalog_fetch_version')
    readonly_fields = ('last_fetched_at', 'last_fetch_version', 'catalog_fetched_at', 'catalog_fetch_version')

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not SiteTeamSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class SiteCatalogStoryInline(SuperuserOnlyAdminMixin, admin.TabularInline):
    model = SiteCatalogStory
    extra = 0
    fields = ('position', 'title', 'author', 'genres', 'tags', 'text')
    raw_id_fields = ('text',)
    formfield_overrides = {models.TextField: {'widget': forms.Textarea(attrs={'rows': 3, 'cols': 28})}}


@admin.register(SiteCatalogAnthology)
class SiteCatalogAnthologyAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('title', 'cms_link', 'url', 'story_count', 'position', 'is_enabled', 'updated_at')
    list_editable = ('position', 'is_enabled')
    ordering = ('position', 'pk')
    search_fields = ('title', 'stories__title', 'stories__author')
    fields = ('title', 'anthology', 'url', 'position', 'is_enabled', 'updated_at', 'preview_link')
    readonly_fields = ('updated_at', 'preview_link')
    inlines = (SiteCatalogStoryInline,)
    save_on_top = True

    @admin.display(description='powiązanie z CMS-em', ordering='anthology__title')
    def cms_link(self, obj):
        return obj.anthology.title if obj.anthology_id else '–'

    @admin.display(description='opowiadania')
    def story_count(self, obj):
        return len(obj.stories.all())

    @admin.display(description='podgląd')
    def preview_link(self, obj):
        if not obj.pk:
            return '–'
        return format_html('<a href="{}#katalog-{}">Zakładka System → Szukajka WWW</a>',
                           reverse('core:site_catalog'), obj.pk)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('anthology').prefetch_related('stories')
