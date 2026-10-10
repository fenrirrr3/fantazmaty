from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from core.models import AudioDescription, AudioDescriptionNote


class AudioDescriptionAdminForm(forms.ModelForm):
    class Meta:
        model = AudioDescription
        fields = ("controllers", "stage", "content")
        widgets = {"controllers": forms.SelectMultiple(attrs={"data-person-multiple": "true"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ta sama lista osób co na stronie Audiodeskrypcji (także osoby bez konta).
        from core.anthology_tasks import task_people
        current = list(self.instance.controllers.values_list("pk", flat=True)) if self.instance.pk else []
        self.fields["controllers"].queryset = task_people(*current)


class AudioDescriptionNoteInline(admin.TabularInline):
    model = AudioDescriptionNote
    extra = 0
    fields = ("author_name", "created_at", "content")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AudioDescription)
class AudioDescriptionAdmin(admin.ModelAdmin):
    form = AudioDescriptionAdminForm
    list_display = ("anthology", "task_link", "stage")
    list_filter = ("stage",)
    search_fields = ("anthology__title",)
    list_select_related = ("anthology",)
    readonly_fields = ("anthology", "task_link")
    fields = ("anthology", "task_link", "controllers", "stage", "content")
    inlines = (AudioDescriptionNoteInline,)

    class Media:
        js = ("core/person-multiselect.js",)
        css = {"all": ("core/person-multiselect.css",)}

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
            and not obj.content.strip()
            and obj.stage == "writing"
            and not obj.notes.exists()
        )
