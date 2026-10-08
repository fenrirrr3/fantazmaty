from django.contrib import admin

from .editing import AssignmentModelForm
from .models import CoverProposal, Illustration, Illustrator
from core.permissions import can_view_illustrations
from authors.admin import SuperuserOnlyAdminMixin
from .models import PublicIllustrationSettings


@admin.register(PublicIllustrationSettings)
class PublicIllustrationSettingsAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    fields = ('drive_url',)

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not PublicIllustrationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Illustrator)
class IllustratorAdmin(admin.ModelAdmin):
    list_display = ("full_name", "pseudonym", "email", "portfolio", "preferences", "covers", "is_active")
    list_filter = ("is_active", "covers")
    search_fields = ("first_name__plcontains", "last_name__plcontains", "pseudonym__plcontains", "email__plcontains", "preferences__plcontains")
    ordering = ("last_name", "first_name", "pk")
    fields = ("first_name", "last_name", "pseudonym", "email", "portfolio", "preferences", "covers", "is_active")
    list_per_page = 50
    show_full_result_count = False

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or not obj.illustrations.exists())

    def get_deleted_objects(self, objs, request):
        deleted, counts, permissions, protected = super().get_deleted_objects(objs, request)
        protected.extend(str(obj) for obj in objs if obj.illustrations.exists())
        return deleted, counts, permissions, protected

    @admin.display(description="Imię i nazwisko", ordering="last_name")
    def full_name(self, obj):
        return str(obj)


@admin.register(Illustration)
class IllustrationAdmin(admin.ModelAdmin):
    form = AssignmentModelForm

    class Media:
        css = {'all': ('core/illustration-status.css',)}

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        from django.utils.html import format_html
        return format_html('<span class="illustration-status illustration-status-{}">{}</span>', obj.status, obj.get_status_display())

    def has_module_permission(self, request):
        return can_view_illustrations(request.user) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return can_view_illustrations(request.user) and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return can_view_illustrations(request.user) and super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        return can_view_illustrations(request.user) and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return can_view_illustrations(request.user) and super().has_delete_permission(request, obj)

    list_display = (
        "display_title",
        "display_anthology",
        "display_authors",
        "display_illustrator",
        "status_badge",
        "assigned_at",
    )

    list_filter = (
        "status",
        "text__anthology",
        "illustrators",
        "assigned_at",
    )

    @admin.display(description='Ilustratorzy')
    def display_illustrator(self, obj):
        return obj.illustrator_display or 'Nie przypisano'

    search_fields = (
        "text__title__plcontains",
        "text__authors__first_name__plcontains",
        "text__authors__last_name__plcontains",
        "text__authors__pseudonym__plcontains",
        "text__authors__email__plcontains",
        "illustrators__first_name__plcontains",
        "illustrators__last_name__plcontains",
        "illustrators__pseudonym__plcontains",
        "illustrators__email__plcontains",
        "trigger_warnings__plcontains",
        "illustrated_excerpt__plcontains",
        "manual_illustrator_name__plcontains",
        "manual_illustrator_email__plcontains",
    )

    autocomplete_fields = (
        "text",
        "illustrators",
    )

    readonly_fields = (
        "assigned_at",
        "display_anthology",
        "display_authors",
    )

    fieldsets = (
        (
            "Opowiadanie",
            {
                "fields": (
                    "text",
                    "display_anthology",
                    "display_authors",
                    "story_url",
                ),
            },
        ),
        (
            "Przypisanie",
            {
                "fields": (
                    "illustrators",
                    "manual_illustrator_name",
                    "manual_illustrator_email",
                    "status",
                    "assigned_at",
                ),
            },
        ),
        (
            "Informacje dodatkowe",
            {
                "fields": (
                    "trigger_warnings",
                    "coordinator_notes",
                ),
            },
        ),
        (
            "Ilustrowany fragment",
            {
                "classes": (
                    "collapse",
                ),
                "fields": (
                    "illustrated_excerpt",
                ),
            },
        ),
    )

    ordering = (
        "text__anthology__title",
        "text__title",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "text",
                "text__anthology",
            )
            .prefetch_related(
                "illustrators",
                "text__authors",
            )
        )

    @admin.display(
        description="tytuł",
        ordering="text__title",
    )
    def display_title(self, obj):
        return obj.text.title

    @admin.display(
        description="antologia",
        ordering="text__anthology__title",
    )
    def display_anthology(self, obj):
        if obj.text.anthology:
            return obj.text.anthology.title

        return "–"

    @admin.display(description="autorzy")
    def display_authors(self, obj):
        return obj.authors_display or "–"

@admin.register(CoverProposal)
class CoverProposalAdmin(admin.ModelAdmin):
    list_display = (
        "illustration_author",
        "submitted_by",
        "submitted_at",
        "status",
        "status_changed_at",
    )

    list_filter = (
        "status",
        "submitted_at",
        "status_changed_at",
    )

    search_fields = (
        "illustration_author__plcontains",
        "illustration_url__plcontains",
        "submitted_by__username__plcontains",
        "submitted_by__first_name__plcontains",
        "submitted_by__last_name__plcontains",
        "submitted_by__email__plcontains",
    )

    readonly_fields = (
        "submitted_by",
        "submitted_at",
        "status_changed_at",
    )

    list_select_related = (
        "submitted_by",
    )

    ordering = (
        "-submitted_at",
        "-pk",
    )
