from workflow.anthology_policy import require_working_anthology
from core.workflow_events import track_workflow
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from core.permissions import (
    get_active_person_profile,
    has_role,
    is_coordinator,
    require_coordinator,
    require_superuser,
    require_team_member,
)
from texts.models import Text, TextNote
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import ROLE_GROUPS, STAGE_ROLES, validate_assignment_start_date


StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role

MAX_DATABASE_ID = 9_223_372_036_854_775_807

TERMINAL_STAGES = frozenset({StageType.READY, StageType.WITHDRAWN})

STAGE_ROLE_MAP = {
    **STAGE_ROLES,
    StageType.READY_FOR_EDITING: Role.EDITOR,
    StageType.EDITING: Role.EDITOR,
    StageType.AUTHOR_EDITING: Role.EDITOR,
    StageType.EDITOR_CONTROL: Role.EDITOR,
}

STAGE_ORDER = {
    kind: position
    for position, kind in enumerate(
        (
            StageType.READY_FOR_EDITING,
            StageType.EDITING,
            StageType.FIRST_VERIFICATION,
            StageType.AUTHOR_EDITING,
            StageType.SECOND_VERIFICATION,
            StageType.EDITING_CONTROL,
            StageType.FIRST_PROOFREADING,
            StageType.SECOND_PROOFREADING,
            StageType.THIRD_VERIFICATION,
            StageType.COORDINATOR_CONTROL,
            StageType.EDITOR_CONTROL,
            StageType.THIRD_PROOFREADING,
            StageType.FOURTH_PROOFREADING,
            StageType.STYLING,
            StageType.READY,
            StageType.WITHDRAWN,
        )
    )
}


def _current_stages(text):
    return list(
        WorkflowStage.objects.select_for_update()
        .filter(
            text_id=text.pk,
            workflow_cycle=text.current_workflow_cycle,
            is_current=True,
        )
        .order_by("pk")
    )


def _current_assignments(text):
    return list(
        WorkflowRoleAssignment.objects.select_for_update()
        .filter(
            text_id=text.pk,
            workflow_cycle=text.current_workflow_cycle,
            is_current=True,
        )
        .order_by("pk")
    )


def _require_open_process(stages):
    if any(stage.stage_type in TERMINAL_STAGES for stage in stages):
        raise ValidationError(
            "Nie można zmieniać przydziałów ani rozpoczynać pracy "
            "nad tekstem gotowym lub wycofanym."
        )


def _require_eligible_assignee(user, role, *, lock=True, existing=False):
    from people.leave_access import require_available
    require_available(user, lock=lock)
    if role == Role.STYLING and not (user and user.is_active and user.is_superuser):
        raise PermissionDenied("Stylowanie można przypisać tylko superuserowi.")
    if not (user and user.is_active and user.is_superuser) and get_active_person_profile(user) is None:
        raise ValidationError(
            "Osoba przypisywana do zadania musi mieć aktywne konto "
            "powiązane z aktywnym członkiem zespołu."
        )

    from workflow.availability import can_claim_fourth_proofreading
    if role in (Role.PROOFREADER_2, Role.PROOFREADER_4) and not can_claim_fourth_proofreading(user):
        raise PermissionDenied("Drugą i czwartą korektę można przypisać tylko koordynatorowi korekty.")

    required_group = ROLE_GROUPS.get(role)
    if role == Role.EDITOR:
        required_group = "Redaktor"

    if not required_group:
        raise ValidationError("Nieprawidłowa rola w procesie.")

    if not existing and not is_coordinator(user) and not has_role(user, required_group):
        raise ValidationError(
            "Wybrana osoba nie ma roli wymaganej do wykonania zadania."
        )


def _require_distinct_verifiers(assignments, role, user_id):
    conflicting_role = {
        Role.VERIFIER_1: Role.VERIFIER_2,
        Role.VERIFIER_2: Role.VERIFIER_1,
    }.get(role)

    if conflicting_role and any(
        assignment.role == conflicting_role
        and assignment.assigned_to_id == user_id
        for assignment in assignments
    ):
        raise ValidationError(
            "Pierwszą i drugą weryfikację muszą wykonywać różne osoby."
        )


def _require_start_order(stage, stages):
    from workflow.repetitions import require_released
    require_released(stage)
    if stage.repetition_id:
        return
    open_others = [
        item
        for item in stages
        if item.pk != stage.pk
        and not item.is_completed
        and item.ended_at is None
    ]

    if stage.stage_type == StageType.EDITING:
        if any(
            (item.stage_type in {StageType.SECOND_VERIFICATION, StageType.AUTHOR_EDITING}
             or (item.stage_type == StageType.FIRST_VERIFICATION
                 and (item.started_at is not None or any(
                     previous.stage_type == StageType.EDITING and previous.is_completed
                     for previous in stages))))
            for item in open_others
        ):
            raise ValidationError(
                "Nie można rozpocząć redakcji podczas oczekiwania "
                "na weryfikację lub pracę autora."
            )
        if any(
            STAGE_ORDER.get(item.stage_type, -1)
            >= STAGE_ORDER[StageType.EDITING_CONTROL]
            for item in stages
        ):
            raise ValidationError("Proces przeszedł już do dalszych etapów.")
        return

    if stage.stage_type == StageType.READY_FOR_EDITING:
        if any(
            item.stage_type != StageType.FIRST_VERIFICATION
            or item.started_at is not None
            for item in open_others
        ):
            raise ValidationError("Tekst ma już rozpoczęty lub przygotowany proces.")
        return

    if any(
        STAGE_ORDER.get(item.stage_type, -1)
        <= STAGE_ORDER.get(stage.stage_type, -1)
        for item in open_others
    ):
        raise ValidationError(
            "Najpierw zakończ wcześniejszy etap bieżącego cyklu."
        )


@transaction.atomic
@track_workflow
def start_assigned_stage(*, user, stage_id, started_at, allow_past=False):
    require_team_member(user)
    if allow_past:
        require_superuser(user)
        from workflow.services import _validate_date
        transition_date = _validate_date(started_at)
        if transition_date >= timezone.localdate():
            validate_assignment_start_date(transition_date)
    else:
        transition_date = validate_assignment_start_date(started_at)

    text_id = get_object_or_404(
        WorkflowStage.objects.only("text_id"),
        pk=stage_id,
    ).text_id

    text = get_object_or_404(
        Text.objects.select_for_update(),
        pk=text_id,
    )
    require_working_anthology(text)
    stages = _current_stages(text)
    assignments = _current_assignments(text)

    stage = next((item for item in stages if item.pk == stage_id), None)
    if stage is None:
        raise ValidationError(
            "Etap nie należy już do bieżącego cyklu. Odśwież stronę."
        )

    _require_open_process(stages)

    if stage.is_completed or stage.ended_at is not None:
        raise ValidationError("Ten etap został już zakończony.")
    if stage.started_at is not None:
        raise ValidationError("Data rozpoczęcia tego etapu jest już zapisana.")
    if stage.stage_type == StageType.AUTHOR_EDITING and not stage.repetition_id and not (text.current_workflow_cycle > 1 and len(stages) == 1):
        raise ValidationError(
            "Pracę autora rozpoczyna się przez przekazanie tekstu autorowi."
        )

    role = STAGE_ROLE_MAP.get(stage.stage_type)
    if role == Role.STYLING and not user.is_superuser:
        raise PermissionDenied("Stylowanie może rozpocząć tylko superuser.")
    assignment = next(
        (item for item in assignments if item.role == role),
        None,
    )
    if assignment is None or assignment.assigned_to_id is None:
        raise ValidationError("Najpierw przypisz osobę do tego etapu.")

    if stage.stage_type == StageType.EDITOR_CONTROL:
        allowed = assignment.assigned_to_id == user.pk or user.is_superuser
    else:
        allowed = (
            assignment.assigned_to_id == user.pk
            or is_coordinator(user)
        )

    if not allowed:
        raise PermissionDenied(
            "Nie możesz rozpocząć etapu przypisanego do innej osoby."
        )

    from workflow.availability import ensure_distinct_proofreader
    ensure_distinct_proofreader(text, role, assignment.assigned_to)
    _require_eligible_assignee(assignment.assigned_to, role, existing=True)
    _require_distinct_verifiers(assignments, role, assignment.assigned_to_id)
    _require_start_order(stage, stages)

    previous_end_dates = [
        item.ended_at
        for item in stages
        if item.is_completed and item.ended_at is not None
    ]
    if previous_end_dates and transition_date < max(previous_end_dates):
        raise ValidationError(
            "Rozpoczęcie nie może poprzedzać zakończenia wcześniejszych prac."
        )

    if stage.stage_type == StageType.READY_FOR_EDITING:
        # Rezerwacja redaktora już istnieje. Zachowujemy jej właściciela
        # również wtedy, gdy rozpoczęcie zapisuje koordynator.
        stage.stage_type = StageType.EDITING
        stage.iteration = 1 + max(
            (
                item.iteration
                for item in stages
                if item.stage_type == StageType.EDITING and item.pk != stage.pk
            ),
            default=0,
        )
        stage.started_at = transition_date
        stage.full_clean()
        stage.save(update_fields=["stage_type", "iteration", "started_at"])

        if not any(
            item.stage_type == StageType.FIRST_VERIFICATION
            for item in stages
        ):
            verification = WorkflowStage(
                text=text,
                workflow_cycle=text.current_workflow_cycle,
                stage_type=StageType.FIRST_VERIFICATION,
                iteration=1,
            )
            verification.full_clean()
            verification.save()

        return stage

    stage.started_at = transition_date
    stage.full_clean()
    stage.save(update_fields=["started_at"])
    if stage.stage_type == StageType.EDITING and not stage.repetition_id and len(stages) == 1:
        from workflow.services import create_pending_stage
        create_pending_stage(text, StageType.FIRST_VERIFICATION)
    return stage


@transaction.atomic
@track_workflow
def withdraw_text(*, user, text_id):
    require_coordinator(user)

    text = get_object_or_404(
        Text.objects.select_for_update(),
        pk=text_id,
    )
    require_working_anthology(text)
    stages = _current_stages(text)

    existing = next(
        (
            stage
            for stage in stages
            if stage.stage_type == StageType.WITHDRAWN
        ),
        None,
    )
    if existing is not None:
        return existing

    if any(stage.stage_type == StageType.READY for stage in stages):
        raise ValidationError(
            "Gotowego tekstu nie można wycofać z zakończonego procesu."
        )

    from core.workflow_events import remember
    remember(Text, text, text._state.db or 'default')
    for run in text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True):
        run.canceled_at = timezone.now()
        run.canceled_by = user
        run.cancellation_reason = 'Wycofano tekst.'
        run.save(update_fields=['canceled_at', 'canceled_by', 'cancellation_reason'])
        run.stages.filter(is_completed=False).update(is_current=False, is_released=False)
        run.assignments.update(is_current=False)

    # Nie oznaczamy niezakończonych zadań jako wykonanych.
    # Selektory pomijają cały wycofany cykl w kolejkach pracy,
    # a serwisy procesu blokują jego dalsze przejścia.
    stage = WorkflowStage(
        text=text,
        workflow_cycle=text.current_workflow_cycle,
        stage_type=StageType.WITHDRAWN,
        iteration=1,
        started_at=timezone.localdate(),
        ended_at=None,
        is_completed=False,
    )
    stage.full_clean()
    stage.save()
    return stage

@transaction.atomic
@track_workflow
def change_scheduled_stage(*, user, stage_id, started_at=None, cancel=False):
    """Change a future booking without rewriting work that has already happened."""
    require_team_member(user)
    text_id = get_object_or_404(WorkflowStage.objects.only('text_id'), pk=stage_id).text_id
    text = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
    require_working_anthology(text)
    stages = _current_stages(text)
    assignments = _current_assignments(text)
    _require_open_process(stages)
    stage = next((s for s in stages if s.pk == stage_id), None)
    today = timezone.localdate()
    if not stage or stage.is_completed or stage.ended_at or not stage.started_at or stage.started_at <= today:
        raise ValidationError('Zmienić można wyłącznie rezerwację z przyszłą datą rozpoczęcia.')
    role = STAGE_ROLE_MAP.get(stage.stage_type)
    assignment = next((a for a in assignments if a.role == role), None)
    if not assignment or not assignment.assigned_to_id:
        raise ValidationError('Etap nie ma przypisanego wykonawcy.')
    if assignment.assigned_to_id != user.pk and not is_coordinator(user):
        raise PermissionDenied('Nie możesz zmieniać cudzej rezerwacji.')
    if role == Role.STYLING and not user.is_superuser:
        raise PermissionDenied('Stylowaniem zarządza superuser.')
    if cancel:
        stage.started_at = None
        prior_work = any(s.pk != stage.pk and s.assignment_id == assignment.pk and STAGE_ROLE_MAP.get(s.stage_type) == role and (s.started_at or s.is_completed) for s in stages)
        # Keep a historical assignee when this is a later return to the same role.
        if not prior_work:
            assignment.assigned_to = None
            assignment.assigned_at = None
            assignment.save(update_fields=['assigned_to', 'assigned_at'])
            if stage.stage_type == StageType.EDITING and not stage.repetition_id:
                stage.stage_type = StageType.READY_FOR_EDITING
                stage.iteration = 1 + max((s.iteration for s in stages if s.stage_type == StageType.READY_FOR_EDITING), default=0)
        stage.save(update_fields=['started_at', 'stage_type', 'iteration'])
    else:
        from workflow.availability import ensure_distinct_proofreader
        ensure_distinct_proofreader(text, role, assignment.assigned_to)
        _require_eligible_assignee(assignment.assigned_to, role, existing=True)
        date = validate_assignment_start_date(started_at)
        previous = [s.ended_at for s in stages if s.is_completed and s.ended_at]
        if previous and date < max(previous):
            raise ValidationError('Data nie może poprzedzać zakończenia wcześniejszych prac.')
        stage.started_at = date
        stage.save(update_fields=['started_at'])
    return stage
