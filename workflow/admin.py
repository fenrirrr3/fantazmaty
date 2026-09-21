from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from texts.models import Text

from .models import WorkflowRoleAssignment, WorkflowStage


class WorkflowCycleAdminFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if "workflow_cycle" not in self.fields:
            return
        if self.instance and self.instance.pk:
            self.fields["workflow_cycle"].initial = (
                self.instance.workflow_cycle
            )
        else:
            self.fields["workflow_cycle"].initial = 1

    def clean_workflow_cycle(self):
        workflow_cycle = self.cleaned_data["workflow_cycle"]
        self.instance.workflow_cycle = workflow_cycle

        return workflow_cycle

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.workflow_cycle = self.cleaned_data.get("workflow_cycle", instance.workflow_cycle)

        if commit:
            instance.save()
            self.save_m2m()

        return instance


class WorkflowStageAdminForm(
    WorkflowCycleAdminFormMixin,
    forms.ModelForm,
):
    workflow_cycle = forms.IntegerField(
        label="Przebieg workflow",
        min_value=1,
        help_text=(
            "Techniczny numer przebiegu. Zmieniaj go ręcznie "
            "tylko w celu poprawienia błędnego przypisania "
            "rekordu."
        ),
    )

    confirm_data_correction = forms.BooleanField(required=False, label="Potwierdzam ręczną korektę danych (bez przejścia do następnego etapu)")

    def clean(self):
        data = super().clean()
        state_fields = {'text', 'workflow_cycle', 'stage_type', 'iteration', 'started_at', 'ended_at', 'is_completed'}
        if set(self.changed_data) & state_fields and not data.get('confirm_data_correction'):
            raise forms.ValidationError("Do rozpoczęcia lub zakończenia pracy użyj akcji na liście etapów. Ręczna korekta wymaga potwierdzenia i nie tworzy następnego etapu.")
        return data

    class Meta:
        model = WorkflowStage
        fields = "__all__"


class WorkflowRoleAssignmentAdminForm(
    WorkflowCycleAdminFormMixin,
    forms.ModelForm,
):
    workflow_cycle = forms.IntegerField(
        label="Przebieg workflow",
        min_value=1,
        help_text=(
            "Techniczny numer przebiegu. Zmieniaj go ręcznie "
            "tylko w celu poprawienia błędnego przypisania "
            "rekordu."
        ),
    )

    def clean(self):
        data = super().clean()
        from workflow.admin_assignment_rules import protect_assignment
        protect_assignment(self.instance, data)
        return data

    class Meta:
        model = WorkflowRoleAssignment
        fields = "__all__"



@admin.register(WorkflowStage)
class WorkflowStageAdmin(admin.ModelAdmin):
    readonly_fields = tuple(field.name for field in WorkflowStage._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    form = WorkflowStageAdminForm
    exclude = ("workflow_cycle",)
    empty_value_display = "–"

    list_display = (
        "text",
        "stage_type",
        "workflow_cycle",
        "is_current_cycle",
        "execution_number",
        "iteration",
        "started_at",
        "ended_at",
        "is_completed",
    )
    
    list_filter = (
        "workflow_cycle",
        "stage_type",
        "is_completed",
        "started_at",
        "ended_at",
    )

    search_fields = (
        "text__title__plcontains",
        "text__authors__first_name__plcontains",
        "text__authors__last_name__plcontains",
        "text__authors__pseudonym__plcontains",
        "text__authors__email__plcontains",
        "text__anthology__title__plcontains",
    )

    autocomplete_fields = (
        "text",
    )

    fieldsets = (
        (
            "Etap",
            {
                "fields": (
                    "text",
                    "workflow_cycle",
                    "stage_type",
                    "execution_number", "repetition", "is_released", "assignment",
                    "iteration",
                ),
            },
        ),
        (
            "Realizacja",
            {
                "fields": (
                    "started_at",
                    "ended_at",
                    "is_completed", "imported_completed",
                ),
            },
        ),
    )

    ordering = (
        "text__title",
        "-workflow_cycle",
        "-started_at",
        "stage_type",
        "iteration",
    )

    list_select_related = (
        "text",
        "text__anthology",
    )

    actions = (
        "finish_selected_stages", "start_selected_stages",
    )

    @admin.action(description="Zakończ etap i utwórz następny (dzisiaj)")
    def finish_selected_stages(self, request, queryset):
        from workflow.services import complete_stage
        from django.core.exceptions import ValidationError, PermissionDenied
        try:
            with transaction.atomic():
                for stage in queryset.order_by('text_id', 'pk'):
                    complete_stage(stage, request.user, timezone.localdate())
        except (ValidationError, PermissionDenied) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, "Zakończono etapy i utworzono następne.")

    @admin.action(description="Rozpocznij przypisany etap (dzisiaj)")
    def start_selected_stages(self, request, queryset):
        from core.services.texts import start_assigned_stage
        from django.core.exceptions import ValidationError, PermissionDenied
        try:
            with transaction.atomic():
                for stage in queryset.order_by('text_id', 'pk'):
                    start_assigned_stage(user=request.user, stage_id=stage.pk, started_at=timezone.localdate())
        except (ValidationError, PermissionDenied) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, "Rozpoczęto etapy.")

    @admin.display(boolean=True, description="Bieżący przebieg")
    def is_current_cycle(self, obj):
        return (
            obj.is_current and obj.workflow_cycle
            == obj.text.current_workflow_cycle
        )



@admin.register(WorkflowRoleAssignment)
class WorkflowRoleAssignmentAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in WorkflowRoleAssignment._meta.fields if field.name != "notes")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        from workflow.admin_assignment_rules import has_recorded_work
        return False

    form = WorkflowRoleAssignmentAdminForm
    exclude = ("workflow_cycle",)
    empty_value_display = "Nieprzypisane"

    list_display = (
        "text",
        "role",
        "assigned_to",
        "workflow_cycle",
        "is_current_cycle",
        "assigned_at",
    )

    list_filter = (
        "workflow_cycle",
        "role",
        "assigned_at",
    )

    search_fields = (
        "text__title__plcontains",
        "text__authors__first_name__plcontains",
        "text__authors__last_name__plcontains",
        "text__authors__pseudonym__plcontains",
        "text__authors__email__plcontains",
        "assigned_to__username__plcontains",
        "assigned_to__first_name__plcontains",
        "assigned_to__last_name__plcontains",
        "assigned_to__email__plcontains",
    )

    autocomplete_fields = (
        "text",
        "assigned_to",
    )

    readonly_fields = (
        "assigned_at",
    )

    fieldsets = (
        (
            "Przypisanie",
            {
                "fields": (
                    "text",
                    "workflow_cycle",
                    "role",
                    "assigned_to",
                    "assigned_at",
                ),
            },
        ),
        (
            "Informacje dodatkowe",
            {
                "fields": (
                    "notes",
                ),
            },
        ),
    )

    ordering = (
        "text__title",
        "-workflow_cycle",
        "role",
    )

    list_select_related = (
        "text",
        "text__anthology",
        "assigned_to",
    )

    actions = ()

    @admin.display(
        boolean=True,
        description="Bieżący przebieg",
    )
    def is_current_cycle(self, obj):
        return (
            obj.is_current and obj.workflow_cycle
            == obj.text.current_workflow_cycle
        )



from workflow.models import WorkflowRepetition

@admin.register(WorkflowRepetition)
class WorkflowRepetitionAdmin(admin.ModelAdmin):
    list_display = ('text', 'created_at', 'created_by', 'completed_at')
    list_select_related = ('text', 'created_by')
    readonly_fields = tuple(f.name for f in WorkflowRepetition._meta.fields)
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False


from workflow.models import WorkflowHandoff
@admin.register(WorkflowHandoff)
class WorkflowHandoffAdmin(admin.ModelAdmin):
    list_display = ('text','stage','actor','created_at')
    readonly_fields = tuple(f.name for f in WorkflowHandoff._meta.fields)
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False
