from workflow.catalog import active_stage_choices
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
        WorkflowStage.objects.select_related("text").current_cycle().filter(
            is_released=True, workflow_cycle=F("text__current_workflow_cycle"),
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
        is_current=True, is_released=True,
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
        with transaction.atomic():
            text = Text.objects.select_for_update().get(pk=text_id)
            from core.workflow_tokens import check_token
            check_token(request.POST.get('workflow_token', ''), text, request.user)
            transition(text=text, user=request.user, **{date_parameter: timezone.localdate()})
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
            "stage_choices": context.get("stage_choices", active_stage_choices()),
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
    data = request.POST.copy()
    if not data.get("ended_at"):
        data["ended_at"] = timezone.localdate().isoformat()
    form = CompleteStageForm(data, stage=stage)

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
        stage.stage_type == WorkflowStage.StageType.FIRST_VERIFICATION and not stage.repetition_id
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
        return _detail_redirect(stage.text_id)

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
    form = RestartWorkflowForm(request.POST, text=text)
    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return _detail_redirect(text.pk)
    from django.core import signing
    from core.edit_versions import version_of
    from workflow.repetitions import validate_repeat, repeat_stages, sequence
    try:
        selected = validate_repeat(text, form.cleaned_data['stages'])
        if request.POST.get('confirm_restart') != 'yes':
            return render(request, 'core/restart_preview.html', {
                'text': text, 'selected': selected,
                'queue_labels': [dict(WorkflowStage.StageType.choices)[k] for k in sequence(selected)],
                'token': signing.dumps({'user': request.user.pk, 'text': text.pk, 'selected': selected, 'version': version_of(text)}, salt='repeat-preview'),
            })
        payload = signing.loads(request.POST.get('token', ''), salt='repeat-preview', max_age=1800)
        if payload['user'] != request.user.pk or payload['text'] != text.pk or payload['selected'] != selected:
            raise signing.BadSignature()
        repeat_stages(text, selected, request.user, expected_version=payload['version'])
    except (signing.BadSignature, KeyError, TypeError):
        messages.error(request, 'Podgląd wygasł. Przygotuj go ponownie.')
    except ValidationError as error:
        messages.error(request, ' '.join(error.messages))
    else:
        messages.success(request, 'Utworzono kolejkę powtórzeń. Pierwszy etap czeka na nowe przypisanie; historia pozostała zachowana.')
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
        with transaction.atomic():
            text = Text.objects.select_for_update().get(pk=text_id)
            from core.workflow_tokens import check_token
            check_token(request.POST.get('workflow_token', ''), text, request.user)
            withdraw_text_service(user=request.user, text_id=text.pk)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Tekst jest wycofany z procesu.")

    return _detail_redirect(text.pk)


@never_cache
@login_required
@require_POST
@team_member_required
def change_scheduled_workflow_stage(request, stage_id):
    from core.services.texts import change_scheduled_stage
    stage = _get_current_stage(stage_id)
    cancel = request.POST.get('action') == 'cancel'
    form = StartStageForm(request.POST)
    if not cancel and not form.is_valid():
        messages.error(request, _form_error_message(form))
        return _detail_redirect(stage.text_id)
    try:
        change_scheduled_stage(user=request.user, stage_id=stage_id,
                               started_at=None if cancel else form.cleaned_data['started_at'], cancel=cancel)
    except ValidationError as error:
        messages.error(request, ' '.join(error.messages))
    else:
        messages.success(request, 'Odwołano rezerwację terminu.' if cancel else 'Zmieniono datę rozpoczęcia.')
    return _detail_redirect(stage.text_id)


@never_cache
@login_required
@require_POST
@superuser_required
def cancel_workflow_repetition(request, text_id, repetition_id):
    from workflow.repetitions import cancel_repetition
    from workflow.models import WorkflowRepetition
    text=get_object_or_404(Text,pk=text_id)
    try:
        cancel_repetition(text,request.user,repetition_id=repetition_id)
    except (ValidationError, WorkflowRepetition.DoesNotExist) as exc:
        messages.error(request,str(exc))
    else:messages.success(request,'Anulowano powtórzenie. Przywrócono poprzedni stan tekstu.')
    return _detail_redirect(text_id)


@never_cache
@login_required
@superuser_required
def handoff_workflow_stage(request, stage_id):
    from django import forms
    from django.contrib.auth import get_user_model
    from workflow.handoffs import handoff_stage, eligible_handoff_users
    from django.core.exceptions import PermissionDenied
    from django.views.decorators.http import require_http_methods
    if request.method not in ('GET','POST'):
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(['GET','POST'])
    stage=_get_current_stage(stage_id)
    class HandoffForm(forms.Form):
        assigned_to=forms.ModelChoiceField(label='Nowy wykonawca',queryset=eligible_handoff_users(stage))
        expected_assignment_id=forms.IntegerField(widget=forms.HiddenInput)
        reason=forms.CharField(label='Powód przekazania',widget=forms.Textarea(attrs={'rows':3}),max_length=2000)
    form=HandoffForm(request.POST if request.method=='POST' else None,initial={'expected_assignment_id':stage.assignment_id})
    if request.method=='POST' and form.is_valid():
        try:
            handoff_stage(stage.text,request.user,stage_id=stage.pk,assigned_to_id=form.cleaned_data['assigned_to'].pk,expected_assignment_id=form.cleaned_data['expected_assignment_id'],reason=form.cleaned_data['reason'])
        except (ValidationError, PermissionDenied, WorkflowStage.DoesNotExist) as exc:form.add_error(None,str(exc))
        else:
            messages.success(request,'Przekazano pracę. Poprzednie przypisanie pozostało zapisane.')
            return _detail_redirect(stage.text_id)
    return render(request,'core/workflow_handoff.html',{'form':form,'stage':stage})
