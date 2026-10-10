from django import forms
from django.contrib import admin
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html
from people.models import Person
from core.models import AudioDescription


class AudioDescriptionAdminForm(forms.ModelForm):
    class Meta:
        model = AudioDescription
        fields = ("controllers",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.controllers.values("pk") if self.instance.pk else []
        active = Person.objects.active().filter(user__is_active=True).values("pk")
        self.fields["controllers"].queryset = Person.objects.filter(
            Q(pk__in=active) | Q(pk__in=current)
        )


@admin.register(AudioDescription)
class AudioDescriptionAdmin(admin.ModelAdmin):
    form = AudioDescriptionAdminForm
    list_display = ("anthology", "task_link")
    search_fields = ("anthology__title",)
    list_select_related = ("anthology",)
    readonly_fields = ("anthology", "task_link")
    fields = ("anthology", "task_link", "controllers")
    filter_horizontal = ("controllers",)

    @admin.display(description="Wykonawca i status")
    def task_link(self, obj):
        task = obj.anthology.production_tasks.filter(task_type="audio_description").first()
        if task is None:
            return "Brak zadania"
        return format_html(
            '<a href="{}">{} – {}</a>',
            reverse("admin:texts_anthologytask_change", args=[task.pk]),
            task.assigned_to or "Nieprzypisane",
            task.get_status_display(),
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Empty automatically created details must not block parent deletion.
        return bool(
            request.user.is_active
            and request.user.is_superuser
            and obj is not None
            and obj.anthology_id in getattr(request, "_deleting_anthologies", ())
            and not obj.controllers.exists()
        )
