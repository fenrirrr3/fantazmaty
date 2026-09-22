from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from texts.models import Text

from .models import WorkflowRoleAssignment, WorkflowStage
from .catalog import IMPORT_ONLY_STAGE_TYPES, IMPORT_ONLY_ROLES, active_stage_choices, active_role_choices


class OperationalWorkAdminMixin:
    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        match = request.resolver_match
        # Filter the root queryset too, so counters and filter facets agree.
        # Direct history links still allow a superuser to correct known dates.
        if match is None or match.url_name != f"{self.opts.app_label}_{self.opts.model_name}_changelist":
            return queryset
        if self.model is WorkflowStage:
            return queryset.exclude(stage_type__in=IMPORT_ONLY_STAGE_TYPES)
        return queryset.exclude(role__in=IMPORT_ONLY_ROLES)


class ActiveStageFilter(admin.SimpleListFilter):
    title = "etap"
    parameter_name = "stage_type"

    def lookups(self, request, model_admin):
        return active_stage_choices()

    def queryset(self, request, queryset):
        return queryset.filter(stage_type=self.value()) if self.value() else queryset


class ActiveRoleFilter(admin.SimpleListFilter):
    title = "rola"
    parameter_name = "role"

    def lookups(self, request, model_admin):
        return active_role_choices()

    def queryset(self, request, queryset):
        return queryset.filter(role=self.value()) if self.value() else queryset


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
class WorkflowStageAdmin(OperationalWorkAdminMixin, admin.ModelAdmin):
    readonly_fields = (*tuple(field.name for field in WorkflowStage._meta.fields), "edit_execution_link")

    def get_urls(self):
        from django.urls import path
        return [path('<path:object_id>/correct/', self.admin_site.admin_view(self.correct_execution), name='workflow_stage_correct')] + super().get_urls()

    @admin.display(description='Korekta wykonania')
    def edit_execution_link(self,obj):
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Zmień wykonawcę / usuń etap</a>',reverse('admin:workflow_stage_correct',args=[obj.pk]))

    def correct_execution(self,request,object_id):
        from django.contrib.auth import get_user_model
        from django.core.exceptions import PermissionDenied, ValidationError
        from django.db.models.deletion import ProtectedError, RestrictedError
        from django.shortcuts import get_object_or_404,redirect
        from django.template.response import TemplateResponse
        from core.edit_versions import version_of
        from workflow.admin_stage_edit import edit_stage
        if not request.user.is_superuser:raise PermissionDenied
        stage=get_object_or_404(WorkflowStage,pk=object_id)
        class CorrectionForm(forms.Form):
            action=forms.ChoiceField(label='Operacja',choices=[('performer','Zmień wykonawcę tego wykonania'),('delete','Usuń to wykonanie etapu')])
            performer=forms.ModelChoiceField(label='Wykonawca (konto)',queryset=get_user_model().objects.order_by('last_name','first_name','pk'),required=False)
            replacement=forms.ChoiceField(label='Status po usunięciu bieżącego etapu',choices=[('','— nie dotyczy zakończonego wykonania —'),*active_stage_choices()],required=False)
            version=forms.IntegerField(widget=forms.HiddenInput)
            confirm=forms.BooleanField(label='Potwierdzam korektę historii pracy i zmianę statystyk.')
        form=CorrectionForm(request.POST if request.method=='POST' else None,initial={'version':version_of(stage.text),'performer':stage.assignment.assigned_to_id if stage.assignment else None})
        if request.method=='POST' and form.is_valid():
            try:
                edit_stage(stage.pk,request.user,form.cleaned_data['version'],action=form.cleaned_data['action'],performer=form.cleaned_data['performer'],replacement=form.cleaned_data['replacement'])
            except ValidationError as exc:form.add_error(None,exc)
            except (ProtectedError,RestrictedError):form.add_error(None,'Etap ma powiązane przekazania pracy lub inne chronione dane. Nie został usunięty.')
            else:
                self.log_change(request,stage.text,'Korekta wykonania etapu '+str(stage.pk)+': '+form.cleaned_data['action'])
                self.message_user(request,'Zapisano korektę wykonania.')
                return redirect('admin:texts_text_change',stage.text_id)
        return TemplateResponse(request,'admin/workflow/stage_correction.html',{**self.admin_site.each_context(request),'title':'Korekta: '+str(stage),'form':form,'stage':stage})

    def get_readonly_fields(self, request, obj=None):
        if request.user.is_superuser and obj and obj.imported_completed and obj.is_completed:
            return tuple(name for name in self.readonly_fields if name not in {"started_at", "ended_at"})
        return self.readonly_fields

    def get_fieldsets(self, request, obj=None):
        sections = super().get_fieldsets(request, obj)
        if request.user.is_superuser and obj and obj.imported_completed and obj.is_completed:
            return (*sections, ("Korekta danych importowanych", {"fields": ("confirm_data_correction",), "description": "Nieznane daty pozostaw puste. Praca nadal jest zakończona i nie zwiększa bieżącego obciążenia."}))
        return sections

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
        "edit_execution_link",
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
        ActiveStageFilter,
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
                    "stage_type", "edit_execution_link",
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
class WorkflowRoleAssignmentAdmin(OperationalWorkAdminMixin, admin.ModelAdmin):
    def get_urls(self):
        from django.urls import path
        return [path('<path:object_id>/correct/', self.admin_site.admin_view(self.correct_assignment_view), name='workflow_assignment_correct')] + super().get_urls()

    @admin.display(description='Korekta przypisania')
    def correction_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Zmień / odłącz osobę / usuń przypisanie</a>', reverse('admin:workflow_assignment_correct', args=[obj.pk]))

    def correct_assignment_view(self, request, object_id):
        from django.contrib.auth import get_user_model
        from django.core.exceptions import PermissionDenied, ValidationError
        from django.db.models.deletion import ProtectedError, RestrictedError
        from django.shortcuts import get_object_or_404, redirect
        from django.template.response import TemplateResponse
        from core.edit_versions import version_of
        from workflow.admin_assignment_edit import correct_assignment
        if not request.user.is_superuser:
            raise PermissionDenied
        obj = get_object_or_404(WorkflowRoleAssignment, pk=object_id)
        class CorrectionForm(forms.Form):
            action = forms.ChoiceField(label='Operacja', choices=[('performer','Zmień osobę we wszystkich etapach tego przypisania'),('clear','Odłącz osobę, zachowując etapy'),('delete','Usuń puste przypisanie bez etapów')])
            performer = forms.ModelChoiceField(label='Nowy wykonawca', queryset=get_user_model().objects.order_by('last_name','first_name','pk'), required=False)
            version = forms.IntegerField(widget=forms.HiddenInput)
            confirm = forms.BooleanField(label='Potwierdzam korektę wykonawcy, historii i statystyk.')
        form = CorrectionForm(request.POST if request.method == 'POST' else None, initial={'version':version_of(obj.text),'performer':obj.assigned_to_id})
        if request.method == 'POST' and form.is_valid():
            try:
                correct_assignment(obj.pk,request.user,form.cleaned_data['version'],action=form.cleaned_data['action'],performer=form.cleaned_data['performer'])
            except (ProtectedError, RestrictedError):
                form.add_error(None,'Rekord ma chronione powiązania i nie został usunięty.')
            except ValidationError as exc:
                form.add_error(None,exc)
            else:
                self.log_change(request,obj.text,f"Korekta przypisania {obj.pk}: {form.cleaned_data['action']}, osoba {getattr(form.cleaned_data['performer'], 'pk', None)}")
                self.message_user(request,'Zapisano korektę przypisania.')
                return redirect('admin:texts_text_change',obj.text_id)
        return TemplateResponse(request,'admin/workflow/assignment_correction.html',{**self.admin_site.each_context(request),'title':'Korekta: '+str(obj),'form':form,'assignment':obj,'stages':obj.stages.all()})

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in WorkflowRoleAssignment._meta.fields if field.name != "notes") + ("correction_link",)

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
        "correction_link",
        "workflow_cycle",
        "is_current_cycle",
        "assigned_at",
    )

    list_filter = (
        "workflow_cycle",
        ActiveRoleFilter,
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
                    "correction_link",
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
