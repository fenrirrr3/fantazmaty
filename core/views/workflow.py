from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.forms import (
    CompleteStageForm,
    RestartWorkflowForm,
    StartStageForm,
)
from core.pagination import paginate_items
from core.permissions import (
    can_view_author_data,
    coordinator_required,
    superuser_required,
    team_member_required,
)
from core.selectors.texts import workflow_list_context
from core.services.texts import (
    start_assigned_stage,
    withdraw_text as withdraw_text_service,
)
from texts.models import Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import (
    claim_ready_for_editing,
    claim_stage,
    complete_stage,
    finish_editing_to_coordinator,
    restart_workflow_from_stage,
    resume_editing,
    send_text_to_author,
    send_to_first_verification,
    send_to_second_verification,
    start_first_verification,
)


def _detail_redirect(text_id):
    return redirect(
        "core:assigned_text_detail",
        text_id=text_id,
    )


def _form_error_message(form):
    return " ".join(
        str(error)
        for errors in form.errors.values()
        for error in errors
    )


def _get_current_stage(stage_id):
    return get_object_or_404(
        WorkflowStage.objects.select_related("text").filter(
            workflow_cycle=F("text__current_workflow_cycle"),
        ),
        pk=stage_id,
    )


def _lock_current_stage(stage_id):
    """
    Wywoływać wewnątrz transaction.atomic().

    Kolejność blokad jest zgodna z serwisami: najpierw tekst,
    następnie etap. Po uzyskaniu blokady sprawdzamy bieżący cykl.
    """
    text_id = get_object_or_404(
        WorkflowStage.objects.only("text_id"),
        pk=stage_id,
    ).text_id

    text = get_object_or_404(
        Text.objects.select_for_update(),
        pk=text_id,
    )
    stage = get_object_or_404(
        WorkflowStage.objects.select_for_update(),
        pk=stage_id,
        text_id=text.pk,
        workflow_cycle=text.current_workflow_cycle,
    )
    stage.text = text

    return stage


def _perform_text_transition(
    request,
    text_id,
    *,
    transition,
    date_parameter,
    success_message,
):
    text = get_object_or_404(Text, pk=text_id)

    try:
        transition(
            text=text,
            user=request.user,
            **{date_parameter: timezone.localdate()},
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, success_message)

    return _detail_redirect(text.pk)


@never_cache
@login_required
@require_GET
@team_member_required
def workflow_list(request):
    # Selektor stosuje bieżący cykl, filtry oraz sortowanie z białej
    # listy. Dane autorów i wyszukiwanie po nich udostępnia wyłącznie
    # superuserowi, również w zagnieżdżonych elementach kontekstu.
    context = dict(
        workflow_list_context(
            user=request.user,
            params=request.GET,
        )
    )
    page_obj = paginate_items(request, context.pop("stages"))

    context.update(
        {
            "stages": page_obj,
            "page_obj": page_obj,
            "stage_choices": context.get("stage_choices", WorkflowStage.StageType.choices),
            "can_view_authors": can_view_author_data(request.user),
        }
    )

    return render(request, "core/workflow_list.html", context)


@never_cache
@login_required
@require_POST
@team_member_required
def complete_workflow_stage(request, stage_id):
    stage = _get_current_stage(stage_id)
    form = CompleteStageForm(request.POST, stage=stage)

    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return _detail_redirect(stage.text_id)

    try:
        # Serwis ponownie sprawdza cykl, daty, stan etapu i uprawnienia
        # po uzyskaniu blokad. Nie kończy bezpośrednio redakcji ani
        # etapu pracy autora, które mają osobne przejścia.
        complete_stage(
            stage=stage,
            user=request.user,
            ended_at=form.cleaned_data["ended_at"],
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Zakończono etap.")

    return _detail_redirect(stage.text_id)


@never_cache
@login_required
@require_POST
@team_member_required
def start_assigned_workflow_stage(request, stage_id):
    stage = _get_current_stage(stage_id)
    form = StartStageForm(request.POST)

    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return _detail_redirect(stage.text_id)

    try:
        # Serwis blokuje tekst i wskazany etap, sprawdza bieżący cykl
        # oraz istniejący przydział. Rozpoczyna dokładnie wskazany etap;
        # nie zastępuje go inną iteracją i nie przejmuje cudzego zadania.
        # Uwzględnia szczególne reguły redakcji i pierwszej weryfikacji.
        start_assigned_stage(
            user=request.user,
            stage_id=stage.pk,
            started_at=form.cleaned_data["started_at"],
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Zapisano datę rozpoczęcia etapu.")

    return _detail_redirect(stage.text_id)


@never_cache
@login_required
@require_POST
@team_member_required
def take_workflow_stage(request, stage_id):
    stage = _get_current_stage(stage_id)
    is_first_verification = (
        stage.stage_type == WorkflowStage.StageType.FIRST_VERIFICATION
    )

    if is_first_verification:
        # Pierwszy weryfikator rezerwuje zadanie. Rozpoczyna je dopiero
        # po przekazaniu tekstu przez redaktora.
        started_at = None
    else:
        form = StartStageForm(request.POST)

        if not form.is_valid():
            messages.error(request, _form_error_message(form))
            return redirect("core:available_texts")

        started_at = form.cleaned_data["started_at"]

    try:
        with transaction.atomic():
            stage = _lock_current_stage(stage_id)

            if (
                stage.is_completed
                or stage.started_at is not None
                or stage.ended_at is not None
            ):
                raise ValidationError(
                    "Ten etap nie jest już dostępny do przejęcia."
                )

            if stage.stage_type == WorkflowStage.StageType.READY_FOR_EDITING:
                claim_ready_for_editing(
                    text=stage.text,
                    user=request.user,
                    started_at=started_at,
                )
            else:
                claim_stage(
                    text=stage.text,
                    stage_type=stage.stage_type,
                    user=request.user,
                    started_at=started_at,
                )

    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        if is_first_verification:
            messages.success(
                request,
                "Zarezerwowano pierwszą weryfikację. "
                "Zaczekaj na przekazanie tekstu przez redaktora.",
            )
        else:
            messages.success(request, "Przypisano ci etap.")

    return redirect("core:available_texts")


@never_cache
@login_required
@require_POST
@team_member_required
def resume_text_editing(request, text_id):
    return _perform_text_transition(
        request,
        text_id,
        transition=resume_editing,
        date_parameter="started_at",
        success_message="Tekst wrócił do redaktora. Wznowiono redakcję.",
    )


@never_cache
@login_required
@require_POST
@team_member_required
def send_text_to_author_stage(request, text_id):
    return _perform_text_transition(
        request,
        text_id,
        transition=send_text_to_author,
        date_parameter="started_at",
        success_message="Przekazano tekst do pracy autora.",
    )


@never_cache
@login_required
@require_POST
@team_member_required
def send_to_first_verification_stage(request, text_id):
    return _perform_text_transition(
        request,
        text_id,
        transition=send_to_first_verification,
        date_parameter="ended_at",
        success_message="Przekazano tekst do pierwszej weryfikacji.",
    )


@never_cache
@login_required
@require_POST
@team_member_required
def start_first_verification_stage(request, text_id):
    return _perform_text_transition(
        request,
        text_id,
        transition=start_first_verification,
        date_parameter="started_at",
        success_message="Rozpoczęto pierwszą weryfikację.",
    )


@never_cache
@login_required
@require_POST
@team_member_required
def send_to_second_verification_stage(request, text_id):
    return _perform_text_transition(
        request,
        text_id,
        transition=send_to_second_verification,
        date_parameter="started_at",
        success_message="Przekazano tekst do drugiej weryfikacji.",
    )


@never_cache
@login_required
@require_POST
@team_member_required
def finish_text_editing(request, text_id):
    return _perform_text_transition(
        request,
        text_id,
        transition=finish_editing_to_coordinator,
        date_parameter="ended_at",
        success_message=(
            "Zakończono redakcję. "
            "Przekazano tekst do kontroli koordynatora redakcji."
        ),
    )


@never_cache
@login_required
@require_POST
@superuser_required
def restart_text_workflow(request, text_id):
    text = get_object_or_404(Text, pk=text_id)
    form = RestartWorkflowForm(request.POST)

    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return _detail_redirect(text.pk)

    try:
        restart_workflow_from_stage(
            text=text,
            stage_type=form.cleaned_data["target_stage"],
            user=request.user,
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(
            request,
            "Rozpoczęto nowy cykl od wybranego etapu. "
            "Historia poprzedniego cyklu została zachowana.",
        )

    return _detail_redirect(text.pk)


@never_cache
@login_required
@require_POST
@coordinator_required
def withdraw_text(request, text_id):
    text = get_object_or_404(Text, pk=text_id)

    try:
        # Serwis ponownie sprawdza uprawnienia i blokuje tekst.
        # Wycofanie zachowuje historię, blokuje dalszą pracę oraz
        # usuwa tekst z kolejek aktywnych i dostępnych zadań.
        # Ponowienie żądania nie tworzy kolejnego etapu wycofania.
        withdraw_text_service(
            user=request.user,
            text_id=text.pk,
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Tekst jest wycofany z procesu.")

    return _detail_redirect(text.pk)
