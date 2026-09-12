from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.pagination import paginate_items
from core.permissions import can_view_author_data, coordinator_required
from core.selectors.reports import (
    reviewer_activity_context,
    workflow_activity_context,
    workflow_inactivity_context,
)
from texts.models import Review
from workflow.models import WorkflowRoleAssignment, WorkflowStage


PROOFREADER_STAGE_ROLES = {
    WorkflowStage.StageType.FIRST_PROOFREADING: (
        WorkflowRoleAssignment.Role.PROOFREADER_1
    ),
    WorkflowStage.StageType.SECOND_PROOFREADING: (
        WorkflowRoleAssignment.Role.PROOFREADER_2
    ),
    WorkflowStage.StageType.THIRD_PROOFREADING: (
        WorkflowRoleAssignment.Role.PROOFREADER_3
    ),
    WorkflowStage.StageType.FOURTH_PROOFREADING: (
        WorkflowRoleAssignment.Role.PROOFREADER_4
    ),
}

VERIFIER_STAGE_ROLES = {
    WorkflowStage.StageType.FIRST_VERIFICATION: (
        WorkflowRoleAssignment.Role.VERIFIER_1
    ),
    WorkflowStage.StageType.SECOND_VERIFICATION: (
        WorkflowRoleAssignment.Role.VERIFIER_2
    ),
    WorkflowStage.StageType.THIRD_VERIFICATION: (
        WorkflowRoleAssignment.Role.VERIFIER_3
    ),
}

ACTIVE_INACTIVITY_DAYS = 28
WAITING_INACTIVITY_DAYS = 7


# Selektory raportów odpowiadają za:
# - walidację filtrów, zakresów dat i sortowania;
# - dopasowanie przydziału do roli etapu i tego samego cyklu;
# - wykluczenie old_reviews ze wszystkich statystyk recenzji;
# - przygotowanie bezpiecznych danych do wyświetlenia.
#
# Dane autorów nie mogą trafiać do kontekstu koordynatora, również
# przez zagnieżdżone obiekty ORM. Wyszukiwanie po danych autora
# jest dostępne wyłącznie dla superusera.
#
# Filtry osób obejmują aktywnych członków zespołu. Historyczne wyniki
# nie powinny znikać wyłącznie wskutek późniejszej zmiany roli osoby.


def _render_report(
    request,
    *,
    template_name,
    report_context,
    rows_key,
):
    context = dict(report_context)
    page_obj = paginate_items(request, context.pop(rows_key))
    can_view_authors = can_view_author_data(request.user)

    context.update(
        {
            rows_key: page_obj,
            "page_obj": page_obj,
            "can_view_authors": can_view_authors,
            "can_view_author_data": can_view_authors,
            "can_view_reports": True,
        }
    )

    return render(request, template_name, context)


@never_cache
@login_required
@require_GET
@coordinator_required
def proofreader_activity(request):
    context = workflow_activity_context(
        user=request.user,
        params=request.GET,
        stage_roles=dict(PROOFREADER_STAGE_ROLES),
        people_role_name="Korektor",
        people_context_name="proofreaders",
    )

    return _render_report(
        request,
        template_name="core/proofreader_activity.html",
        report_context=context,
        rows_key="activity_rows",
    )


@never_cache
@login_required
@require_GET
@coordinator_required
def verifier_activity(request):
    context = workflow_activity_context(
        user=request.user,
        params=request.GET,
        stage_roles=dict(VERIFIER_STAGE_ROLES),
        people_role_name="Weryfikator",
        people_context_name="verifiers",
    )

    return _render_report(
        request,
        template_name="core/verifier_activity.html",
        report_context=context,
        rows_key="activity_rows",
    )


@never_cache
@login_required
@require_GET
@coordinator_required
def reviewer_activity(request):
    # Selektor korzysta z ReviewAssignment, bez dawnych kolumn
    # reviewer_1...reviewer_6. Każdy przydział jest osobnym rekordem.
    context = dict(
        reviewer_activity_context(
            user=request.user,
            params=request.GET,
        )
    )
    context.setdefault("status_choices", Review.Status.choices)

    return _render_report(
        request,
        template_name="core/reviewer_activity.html",
        report_context=context,
        rows_key="activity_rows",
    )


@never_cache
@login_required
@require_GET
@coordinator_required
def workflow_inactivity(request):
    today = timezone.localdate()

    # Selektor uwzględnia wyłącznie bieżące cykle i pomija teksty
    # gotowe oraz wycofane. Przyszła data rozpoczęcia nie jest
    # przestojem aktywnego etapu. Brak wiarygodnej daty początku
    # oczekiwania nie może być zastępowany datą wymyśloną.
    context = dict(
        workflow_inactivity_context(
            user=request.user,
            params=request.GET,
            today=today,
            active_days=ACTIVE_INACTIVITY_DAYS,
            waiting_days=WAITING_INACTIVITY_DAYS,
        )
    )
    context.update(
        {
            "today": today,
            "stage_choices": WorkflowStage.StageType.choices,
            "active_inactivity_days": ACTIVE_INACTIVITY_DAYS,
            "waiting_inactivity_days": WAITING_INACTIVITY_DAYS,
        }
    )

    return _render_report(
        request,
        template_name="core/workflow_inactivity.html",
        report_context=context,
        rows_key="rows",
    )

@never_cache
@login_required
@require_GET
@coordinator_required
def editor_activity(request):
    stages = WorkflowStage.StageType
    from workflow.models import WorkflowRoleAssignment
    context = workflow_activity_context(
        user=request.user, params=request.GET,
        stage_roles={value: WorkflowRoleAssignment.Role.EDITOR for value in (
            stages.EDITING, stages.AUTHOR_EDITING, stages.EDITOR_CONTROL)},
        people_role_name="Redaktor", people_context_name="editors",
    )
    return _render_report(request, template_name="core/editor_activity.html",
                          report_context=context, rows_key="activity_rows")
