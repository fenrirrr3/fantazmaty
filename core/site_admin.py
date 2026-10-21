"""Panel administracyjny strony WWW: działy ekipy z podgrupami i ustawienia strony „Ekipa”."""
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from authors.admin import SuperuserOnlyAdminMixin
from core.site_models import SiteTeamGroup, SiteTeamSection, SiteTeamSettings


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
        return format_html('<a href="{}#podglad-{}">Zakładka System → Strona WWW</a>', reverse('core:site_www'), obj.slug)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('groups')


@admin.register(SiteTeamSettings)
class SiteTeamSettingsAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    fields = ('lead_editor', 'site_person', 'last_fetched_at', 'last_fetch_version')
    readonly_fields = ('last_fetched_at', 'last_fetch_version')

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not SiteTeamSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
