"""Selective repeats. All writes run under the parent Text lock, never rewrite history."""
from django.core.exceptions import ValidationError, PermissionDenied
from django.db.models import Max, F
from django.utils import timezone
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowRepetition
from workflow.services import (STAGE_ROLES, RESTARTABLE_STAGE_TYPES, _locked_text_operation,
    _ensure_actor, current_stage_queryset, _finish_stage_record, _create_pending_stage,
    _assign_role, _start_stage, user_can_complete_stage)

REPEATABLE = tuple(k for k in RESTARTABLE_STAGE_TYPES if k != S.StageType.READY_FOR_EDITING)


def validate_repeat(text, selected):
    selected = set(selected)
    if not selected or selected - set(REPEATABLE):
        raise ValidationError('Wybierz poprawne etapy do powtórzenia.')
    current = current_stage_queryset(text)
    if text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).exists():
        raise ValidationError('Najpierw zakończ aktualną kolejkę powtórzeń.')
    if current.filter(stage_type=S.StageType.WITHDRAWN).exists():
        raise ValidationError('Wycofanego tekstu nie można zmieniać tym formularzem.')
    recorded = set(S.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle)
                   .exclude(repetition__canceled_at__isnull=False).values_list('stage_type', flat=True))
    if selected - recorded:
        raise ValidationError('Można wybrać tylko etapy zapisane w bieżącym przebiegu tekstu.')
    if S.StageType.AUTHOR_EDITING in selected and S.StageType.EDITING not in selected:
        raise ValidationError('Przekazanie autorowi wymaga także wybrania redakcji.')
    return [k for k in REPEATABLE if k in selected]


def previous_performer_id(text, role):
    return (A.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle,
                             role=role, assigned_to__isnull=False)
            .exclude(repetition__canceled_at__isnull=False)
            .order_by('-is_current', '-execution_number', '-pk')
            .values_list('assigned_to_id', flat=True).first())


def eligible_repeat_users(text, role):
    from django.db.models import Q
    from workflow.availability import eligible_role_users

    users = eligible_role_users(role).exclude(pk=previous_performer_id(text, role))
    if role in (A.Role.PROOFREADER_2, A.Role.PROOFREADER_3):
        first_users = A.objects.filter(text=text, role=A.Role.PROOFREADER_1).filter(
            Q(stages__is_completed=True) | Q(stages__started_at__lte=timezone.localdate())
            | Q(handoffs_from__isnull=False)).exclude(assigned_to=None).values('assigned_to_id')
        users = users.exclude(pk__in=first_users)
    return users.distinct().order_by('last_name', 'first_name', 'pk')


def validate_repeat_assignees(text, selected, assignees, *, lock=False):
    from core.services.texts import _require_eligible_assignee, _require_distinct_verifiers
    from workflow.availability import ensure_distinct_proofreader
    roles = {STAGE_ROLES[kind] for kind in selected}
    if set(assignees) != roles or any(user is None for user in assignees.values()):
        raise ValidationError('Wybierz nową osobę dla każdej wybranej roli.')
    effective = list(A.objects.current_cycle().filter(text=text).exclude(role__in=roles))
    for role, user in assignees.items():
        try:
            _require_eligible_assignee(user, role, lock=lock)
        except PermissionDenied as error:
            raise ValidationError(str(error)) from error
        if user.pk == previous_performer_id(text, role):
            raise ValidationError('W kolejnym wykonaniu wybierz inną osobę niż dotychczasowy wykonawca.')
        ensure_distinct_proofreader(text, role, user)
        effective.append(A(role=role, assigned_to=user))
    for role, user in assignees.items():
        _require_distinct_verifiers(effective, role, user.pk)
    first = assignees.get(A.Role.PROOFREADER_1)
    if first and any(assignees.get(role) == first for role in (A.Role.PROOFREADER_2, A.Role.PROOFREADER_3)):
        raise ValidationError('Pierwszą korektę i drugą lub trzecią korektę muszą wykonywać różne osoby.')
    return assignees


def sequence(selected):
    """An editorial pass resumes after each explicitly selected verification/author exchange."""
    result = []
    for kind in selected:
        result.append(kind)
        if S.StageType.EDITING in selected and kind in (S.StageType.FIRST_VERIFICATION, S.StageType.AUTHOR_EDITING, S.StageType.SECOND_VERIFICATION):
            result.append(S.StageType.EDITING)
    return result


@_locked_text_operation(allow_ready_anthology=True)
def repeat_stages(text, selected, user, *, expected_version=None, assignees=None):
    _ensure_actor(user)
    if not user.is_superuser:
        raise PermissionDenied('Powtórzenie etapów może utworzyć tylko superuser.')
    from core.edit_versions import version_of
    if expected_version is not None and (type(expected_version) is not int or expected_version != version_of(text)):
        raise ValidationError('Stan tekstu zmienił się. Przygotuj podgląd ponownie.')
    selected = validate_repeat(text, selected)
    current = current_stage_queryset(text)
    opened = list(current.filter(is_completed=False).exclude(stage_type__in=(S.StageType.READY, S.StageType.WITHDRAWN)))
    if assignees is not None:
        validate_repeat_assignees(text, selected, assignees, lock=True)
    elif opened:
        raise ValidationError('Przy zmianie trwającej pracy wybierz nowych wykonawców.')
    today = timezone.localdate()
    for stage in opened:
        if stage.started_at and stage.started_at <= today and (
                not stage.assignment_id or not stage.assignment.assigned_to_id):
            raise ValidationError('Rozpoczęty etap nie ma wykonawcy. Najpierw popraw przypisanie.')
    from core.workflow_events import remember
    from texts.models import Text
    remember(Text, text, text._state.db or "default")
    previous_stage_ids = list(current.filter(stage_type__in=[*selected, S.StageType.READY]).values_list('pk', flat=True))
    for stage in opened:
        if stage.started_at and stage.started_at <= today:
            _finish_stage_record(stage, today)
        # A reservation without performed work is retired, not marked completed.
        stage.is_current = False
        stage.is_released = False
        stage.save(update_fields=['is_current', 'is_released'])
    from texts.models import Anthology
    if text.anthology_id:
        anthology = Anthology.objects.select_for_update().get(pk=text.anthology_id)
        if anthology.status == Anthology.Status.READY:
            anthology.status = Anthology.Status.IN_PREPARATION
            anthology.save(update_fields=['status'])
    run = WorkflowRepetition.objects.create(text=text, selected_stages=selected, created_by=user, previous_stage_ids=previous_stage_ids, previous_assignment_ids=list(A.objects.current_cycle().filter(text=text,role__in={STAGE_ROLES[k] for k in selected}).values_list('pk',flat=True)))
    numbers = {kind: (S.objects.filter(text=text, stage_type=kind).aggregate(n=Max('execution_number'))['n'] or 0) + 1 for kind in selected}
    # The terminal marker becomes history, not a competing current status.
    S.objects.current_cycle().filter(text=text, stage_type__in=[*selected, S.StageType.READY]).update(is_current=False)
    assignments = {}
    selected_roles = list(dict.fromkeys(STAGE_ROLES[kind] for kind in selected))
    # Retire all replaced slots before writing the batch, including verifier swaps.
    A.objects.current_cycle().filter(text=text, role__in=selected_roles).update(is_current=False)
    for role in selected_roles:
        number = (A.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, role=role).aggregate(n=Max('execution_number'))['n'] or 0) + 1
        assignments[role] = A.objects.create(text=text, workflow_cycle=text.current_workflow_cycle, role=role, execution_number=number, repetition=run,
                                           assigned_to=assignees.get(role) if assignees else None)
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


def insert_repeat_continuation(stage, kind, *, released=False):
    """Insert a coordinator-requested continuation without losing the chosen queue."""
    run = stage.repetition
    run.stages.filter(queue_position__gt=stage.queue_position).update(queue_position=F('queue_position') + 1)
    assignment = A.objects.current_cycle().filter(text=stage.text, role=STAGE_ROLES[kind]).first()
    number = assignment.execution_number if assignment else 1
    iteration = (S.objects.filter(text=stage.text, workflow_cycle=stage.workflow_cycle,
                                  stage_type=kind).aggregate(n=Max('iteration'))['n'] or 0) + 1
    return S.objects.create(text=stage.text, workflow_cycle=stage.workflow_cycle,
        stage_type=kind, iteration=iteration, execution_number=number, assignment=assignment,
        repetition=run, queue_position=stage.queue_position + 1, is_released=released)


def complete_repeat(stage, user, ended_at, *, send_to_proofreading=None):
    require_released(stage)
    if not user_can_complete_stage(stage, user):
        raise PermissionDenied('Nie możesz zakończyć tego wykonania etapu.')
    from workflow.services import validate_editorial_decision
    validate_editorial_decision(stage, send_to_proofreading)
    _finish_stage_record(stage, ended_at)
    if stage.stage_type == S.StageType.EDITING_CONTROL:
        stage.send_to_proofreading = send_to_proofreading
        stage.save(update_fields=['send_to_proofreading'])
        if not send_to_proofreading:
            insert_repeat_continuation(stage, S.StageType.EDITING_CONTROL)
            insert_repeat_continuation(stage, S.StageType.EDITING)
        else:
            following = stage.repetition.stages.filter(is_completed=False).order_by('queue_position').first()
            if following is None or following.stage_type != S.StageType.FIRST_PROOFREADING:
                insert_repeat_continuation(stage, S.StageType.FIRST_PROOFREADING)
    following = stage.repetition.stages.filter(is_completed=False).order_by('queue_position').first()
    if following:
        following.is_released = True
        following.queued_at = ended_at
        following.save(update_fields=['is_released', 'queued_at'])
    else:
        finish_repetition(stage, ended_at)
    return stage


def finish_repetition(stage, ended_at):
    """A repeat during ordinary work resumes workflow rather than finishing the text."""
    from workflow.services import NEXT_STAGE_TYPES, EDITING_PHASE_TYPES, completed_stage_exists
    run = stage.repetition
    run.completed_at = timezone.now()
    run.save(update_fields=['completed_at'])
    was_ready = S.objects.filter(text=stage.text, pk__in=run.previous_stage_ids,
                                 stage_type=S.StageType.READY).exists()
    if was_ready:
        next_kind = S.StageType.READY
    elif stage.stage_type == S.StageType.EDITING:
        if not completed_stage_exists(stage.text, S.StageType.FIRST_VERIFICATION):
            next_kind = S.StageType.FIRST_VERIFICATION
        elif not completed_stage_exists(stage.text, S.StageType.SECOND_VERIFICATION):
            next_kind = S.StageType.SECOND_VERIFICATION
        else:
            next_kind = S.StageType.EDITING_CONTROL
    elif stage.stage_type == S.StageType.AUTHOR_EDITING:
        next_kind = S.StageType.EDITING
    else:
        next_kind = NEXT_STAGE_TYPES[stage.stage_type]
    while next_kind not in (S.StageType.READY, S.StageType.EDITING, S.StageType.AUTHOR_EDITING):
        if not (S.objects.filter(text=stage.text, workflow_cycle=stage.workflow_cycle,
                                stage_type=next_kind, is_completed=True)
                .exclude(repetition__canceled_at__isnull=False).exists()):
            break
        next_kind = NEXT_STAGE_TYPES[next_kind]
    if next_kind in EDITING_PHASE_TYPES:
        current_stage_queryset(stage.text).exclude(stage_type__in=EDITING_PHASE_TYPES).filter(
            is_completed=True).update(is_current=False, is_released=False)
    next_stage = _create_pending_stage(stage.text, next_kind, queued_at=ended_at)
    if next_stage.assignment_id and next_stage.execution_number != next_stage.assignment.execution_number:
        next_stage.execution_number = next_stage.assignment.execution_number
        next_stage.save(update_fields=['execution_number'])
    if next_kind == S.StageType.READY:
        _start_stage(next_stage, ended_at)
    return next_stage


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
    run.canceled_at=timezone.now()
    run.canceled_by=user
    run.cancellation_reason='Anulowano przed rozpoczęciem pracy.'
    run.save(update_fields=["canceled_at", "canceled_by", "cancellation_reason"])
    return run
