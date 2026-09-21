"""Selective repeats. All writes run under the parent Text lock, never rewrite history."""
from django.core.exceptions import ValidationError, PermissionDenied
from django.db.models import Max
from django.utils import timezone
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowRepetition
from workflow.services import (STAGE_ROLES, RESTARTABLE_STAGE_TYPES, _locked_text_operation,
    _ensure_actor, current_stage_queryset, _finish_stage_record, _create_pending_stage,
    _assign_role, _start_stage, user_can_complete_stage)

REPEATABLE = tuple(k for k in RESTARTABLE_STAGE_TYPES if k != S.StageType.READY_FOR_EDITING)


def validate_repeat(text, selected):
    from workflow.anthology_policy import require_working_anthology
    require_working_anthology(text)
    selected = set(selected)
    if not selected or selected - set(REPEATABLE):
        raise ValidationError('Wybierz poprawne etapy do powtórzenia.')
    current = current_stage_queryset(text)
    if text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).exists():
        raise ValidationError('Najpierw zakończ aktualną kolejkę powtórzeń.')
    # Only a finished text may be reopened. Pending/released/reserved work is still work.
    if current.exclude(stage_type=S.StageType.READY).filter(is_completed=False).exists() or not current.filter(stage_type=S.StageType.READY).exists():
        raise ValidationError('Powtórzenie jest dostępne tylko dla gotowego tekstu bez otwartych etapów i rezerwacji.')
    completed = set(current.filter(is_completed=True).values_list('stage_type', flat=True))
    if selected - completed:
        raise ValidationError('Można powtórzyć tylko etapy wcześniej zakończone w aktualnym workflow.')
    if S.StageType.AUTHOR_EDITING in selected and S.StageType.EDITING not in selected:
        raise ValidationError('Przekazanie autorowi wymaga także wybrania redakcji.')
    return [k for k in REPEATABLE if k in selected]


def sequence(selected):
    """An editorial pass resumes after each explicitly selected verification/author exchange."""
    result = []
    for kind in selected:
        result.append(kind)
        if S.StageType.EDITING in selected and kind in (S.StageType.FIRST_VERIFICATION, S.StageType.AUTHOR_EDITING, S.StageType.SECOND_VERIFICATION):
            result.append(S.StageType.EDITING)
    return result


@_locked_text_operation
def repeat_stages(text, selected, user, *, expected_version=None):
    _ensure_actor(user)
    if not user.is_superuser:
        raise PermissionDenied('Powtórzenie etapów może utworzyć tylko superuser.')
    from core.edit_versions import version_of
    if expected_version is not None and (type(expected_version) is not int or expected_version != version_of(text)):
        raise ValidationError('Stan tekstu zmienił się. Przygotuj podgląd ponownie.')
    selected = validate_repeat(text, selected)
    from core.workflow_events import remember
    from texts.models import Text
    remember(Text, text, text._state.db or "default")
    run = WorkflowRepetition.objects.create(text=text, selected_stages=selected, created_by=user, previous_stage_ids=list(S.objects.current_cycle().filter(text=text, stage_type__in=[*selected, S.StageType.READY]).values_list('pk',flat=True)), previous_assignment_ids=list(A.objects.current_cycle().filter(text=text,role__in={STAGE_ROLES[k] for k in selected}).values_list('pk',flat=True)))
    numbers = {kind: (S.objects.filter(text=text, stage_type=kind).aggregate(n=Max('execution_number'))['n'] or 0) + 1 for kind in selected}
    # The terminal marker becomes history, not a competing current status.
    S.objects.current_cycle().filter(text=text, stage_type__in=[*selected, S.StageType.READY]).update(is_current=False)
    assignments = {}
    for role in {STAGE_ROLES[kind] for kind in selected}:
        old = A.objects.current_cycle().filter(text=text, role=role)
        number = (A.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, role=role).aggregate(n=Max('execution_number'))['n'] or 0) + 1
        old.update(is_current=False)
        assignments[role] = A.objects.create(text=text, workflow_cycle=text.current_workflow_cycle, role=role, execution_number=number, repetition=run)
    for position, kind in enumerate(sequence(selected)):
        iteration = (S.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind).aggregate(n=Max('iteration'))['n'] or 0) + 1
        S.objects.create(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind,
            iteration=iteration, execution_number=numbers[kind], repetition=run,
            queue_position=position, is_released=position == 0, assignment=assignments[STAGE_ROLES[kind]])
    return run


def require_released(stage):
    if not stage.is_current or not stage.is_released:
        raise ValidationError('Ten etap czeka na zakończenie poprzedniego albo należy do historii.')
    if stage.repetition_id and stage.repetition.stages.filter(queue_position__lt=stage.queue_position, is_completed=False).exists():
        raise ValidationError('Najpierw zakończ wcześniejsze etapy kolejki.')


def claim_repeat(text, stage, user, started_at):
    from core.services.texts import _require_eligible_assignee
    require_released(stage)
    _require_eligible_assignee(user, STAGE_ROLES[stage.stage_type])
    _assign_role(text, STAGE_ROLES[stage.stage_type], user)
    stage.refresh_from_db()
    return _start_stage(stage, started_at)


def complete_repeat(stage, user, ended_at):
    require_released(stage)
    if not user_can_complete_stage(stage, user):
        raise PermissionDenied('Nie możesz zakończyć tego wykonania etapu.')
    _finish_stage_record(stage, ended_at)
    following = stage.repetition.stages.filter(is_completed=False).order_by('queue_position').first()
    if following:
        following.is_released = True
        following.save(update_fields=['is_released'])
    else:
        stage.repetition.completed_at = timezone.now()
        stage.repetition.save(update_fields=['completed_at'])
        ready = _create_pending_stage(stage.text, S.StageType.READY)
        _start_stage(ready, ended_at)
    return stage


@_locked_text_operation
def cancel_repetition(text, user, *, repetition_id):
    _ensure_actor(user)
    if not user.is_superuser:
        raise PermissionDenied('Anulowanie jest dostępne tylko dla superusera.')
    run = text.repetitions.get(pk=repetition_id)
    if run.completed_at or run.canceled_at:
        raise ValidationError('Ta kolejka jest już zamknięta.')
    if not run.previous_stage_ids:
        raise ValidationError('Starsza kolejka nie ma zapisanego stanu do przywrócenia; wymaga sprawdzenia przez administratora.')
    if run.stages.filter(started_at__isnull=False).exists() or run.stages.filter(is_completed=True).exists() or run.assignments.filter(assigned_to__isnull=False).exists():
        raise ValidationError('Nie można anulować kolejki z rozpoczętą pracą lub rezerwacją.')
    from core.workflow_events import remember
    from texts.models import Text
    remember(Text,text,text._state.db or 'default')
    run.stages.update(is_current=False, is_released=False)
    run.assignments.update(is_current=False)
    S.objects.filter(text=text,pk__in=run.previous_stage_ids).update(is_current=True)
    A.objects.filter(text=text,pk__in=run.previous_assignment_ids).update(is_current=True)
    run.canceled_at=timezone.now();run.canceled_by=user;run.cancellation_reason='Anulowano przed rozpoczęciem pracy.'
    run.save(update_fields=['canceled_at','canceled_by','cancellation_reason'])
    return run
