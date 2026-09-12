from django.contrib import admin

from .models import CoverProposal, Illustration
from core.permissions import can_view_illustrations


@admin.register(Illustration)
class IllustrationAdmin(admin.ModelAdmin):
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
        "illustrator",
        "status",
        "assigned_at",
    )

    list_filter = (
        "status",
        "text__anthology",
        "illustrator",
        "assigned_at",
    )

    search_fields = (
        "text__title__plcontains",
        "text__authors__first_name__plcontains",
        "text__authors__last_name__plcontains",
        "text__authors__pseudonym__plcontains",
        "text__authors__email__plcontains",
        "illustrator__first_name__plcontains",
        "illustrator__last_name__plcontains",
        "illustrator__email__plcontains",
        "trigger_warnings__plcontains",
        "illustrated_excerpt__plcontains",
    )

    autocomplete_fields = (
        "text",
        "illustrator",
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
                    "illustrator",
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
                "illustrator",
            )
            .prefetch_related(
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
