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
        instance.workflow_cycle = self.cleaned_data[
            "workflow_cycle"
        ]

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

    class Meta:
        model = WorkflowRoleAssignment
        fields = "__all__"


def set_selected_cycles_as_current(
    model_admin,
    request,
    queryset,
):
    if not request.user.is_superuser:
        model_admin.message_user(
            request,
            "Tylko superuser może zmieniać bieżący przebieg tekstu.",
            level=messages.ERROR,
        )
        return

    selected_cycles = {}

    for record in queryset.select_related("text"):
        text_id = record.text_id
        workflow_cycle = record.workflow_cycle

        previous_cycle = selected_cycles.get(text_id)

        if (
            previous_cycle is not None
            and previous_cycle != workflow_cycle
        ):
            model_admin.message_user(
                request,
                (
                    f'Dla tekstu „{record.text.title}” zaznaczono '
                    "rekordy należące do różnych przebiegów. "
                    "Wybierz tylko jeden przebieg tego tekstu."
                ),
                level=messages.ERROR,
            )
            return

        selected_cycles[text_id] = workflow_cycle

    if not selected_cycles:
        model_admin.message_user(
            request,
            "Nie zaznaczono żadnego rekordu.",
            level=messages.WARNING,
        )
        return

    changed_texts = 0

    with transaction.atomic():
        texts = {
            text.pk: text
            for text in (
                Text.objects
                .select_for_update()
                .filter(pk__in=selected_cycles)
            )
        }

        for text_id, workflow_cycle in selected_cycles.items():
            text = texts.get(text_id)

            if text is None:
                continue

            if text.current_workflow_cycle == workflow_cycle:
                continue

            text.current_workflow_cycle = workflow_cycle
            text.save(
                update_fields=["current_workflow_cycle"]
            )

            changed_texts += 1

    if changed_texts:
        model_admin.message_user(
            request,
            (
                "Zmieniono bieżący przebieg dla "
                f"{changed_texts} tekstów."
            ),
            level=messages.SUCCESS,
        )
    else:
        model_admin.message_user(
            request,
            "Wybrane przebiegi były już ustawione jako bieżące.",
            level=messages.INFO,
        )

def get_user_vacation_information(user):
    if user is None:
        return None

    person = getattr(user, "person_profile", None)

    if person is None:
        return None

    today = timezone.localdate()
    now = timezone.now()

    vacation = (
        person.vacations
        .filter(
            Q(until_revoked=True)
            | Q(end_date__gte=now)
        )
        .order_by(
            "start_date",
            "pk",
        )
        .first()
    )

    if vacation is None:
        return None

    if vacation.is_active:
        if vacation.until_revoked:
            description = (
                f"Obecnie na urlopie od "
                f"{vacation.start_date:%d.%m.%Y}, "
                "do odwołania"
            )
        else:
            description = (
                f"Obecnie na urlopie do "
                f"{timezone.localtime(vacation.end_date):%d.%m.%Y, %H:%M}"
            )

        return {
            "vacation": vacation,
            "description": description,
            "is_active": True,
        }

    if vacation.is_upcoming:
        if vacation.until_revoked:
            description = (
                f"Zaplanowany urlop od "
                f"{vacation.start_date:%d.%m.%Y}, "
                "do odwołania"
            )
        else:
            description = (
                f"Zaplanowany urlop od "
                f"{vacation.start_date:%d.%m.%Y} do "
                f"{timezone.localtime(vacation.end_date):%d.%m.%Y, %H:%M}"
            )

        return {
            "vacation": vacation,
            "description": description,
            "is_active": False,
        }

    return None

@admin.register(WorkflowStage)
class WorkflowStageAdmin(admin.ModelAdmin):
    form = WorkflowStageAdminForm
    empty_value_display = "–"

    list_display = (
        "text",
        "stage_type",
        "workflow_cycle",
        "is_current_cycle",
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
                    "is_completed",
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
        "set_cycle_as_current",
    )

    @admin.display(
        boolean=True,
        description="Bieżący przebieg",
    )
    @admin.display(
        description="Urlop przypisanej osoby",
    )
    def assigned_person_leave(self, obj):
        information = get_user_vacation_information(
            obj.assigned_to
        )

        if information is None:
            return "–"

        return information["description"]


    def save_model(
        self,
        request,
        obj,
        form,
        change,
    ):
        previous_user_id = None

        if obj.pk:
            previous_user_id = (
                WorkflowRoleAssignment.objects
                .filter(pk=obj.pk)
                .values_list(
                    "assigned_to_id",
                    flat=True,
                )
                .first()
            )

        super().save_model(
            request,
            obj,
            form,
            change,
        )

        if (
            obj.assigned_to_id
            and obj.assigned_to_id != previous_user_id
        ):
            information = get_user_vacation_information(
                obj.assigned_to
            )

            if information is not None:
                person_name = (
                    obj.assigned_to.get_full_name()
                    or obj.assigned_to.get_username()
                )

                self.message_user(
                    request,
                    (
                        f"Uwaga: osoba „{person_name}” ma urlop. "
                        f'{information["description"]}. '
                        "Przypisanie zostało zapisane."
                    ),
                    level=messages.WARNING,
                )

    def is_current_cycle(self, obj):
        return (
            obj.workflow_cycle
            == obj.text.current_workflow_cycle
        )

    @admin.action(
        description="Ustaw wybrany przebieg jako bieżący"
    )
    def set_cycle_as_current(self, request, queryset):
        set_selected_cycles_as_current(
            self,
            request,
            queryset,
        )


@admin.register(WorkflowRoleAssignment)
class WorkflowRoleAssignmentAdmin(admin.ModelAdmin):
    form = WorkflowRoleAssignmentAdminForm
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

    actions = (
        "set_cycle_as_current",
    )

    @admin.display(
        boolean=True,
        description="Bieżący przebieg",
    )
    def is_current_cycle(self, obj):
        return (
            obj.workflow_cycle
            == obj.text.current_workflow_cycle
        )

    @admin.action(
        description="Ustaw wybrany przebieg jako bieżący"
    )
    def set_cycle_as_current(self, request, queryset):
        set_selected_cycles_as_current(
            self,
            request,
            queryset,
        )
