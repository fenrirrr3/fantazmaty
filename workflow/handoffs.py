from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db.models import Max
from core.permissions import require_coordinator
from core.services.texts import _require_eligible_assignee, _require_distinct_verifiers
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowHandoff
from workflow.services import _locked_text_operation, current_assignment_queryset


@_locked_text_operation
def handoff_stage(text, user, *, stage_id, assigned_to_id, expected_assignment_id, reason):
    require_coordinator(user)
    reason=(reason or '').strip()
    if not reason:raise ValidationError('Podaj powód przekazania pracy.')
    stage=S.objects.get(pk=stage_id,text=text,is_current=True,is_released=True)
    if stage.is_completed or stage.stage_type in ('ready','withdrawn') or S.objects.current_cycle().filter(text=text,stage_type__in=('ready','withdrawn')).exists():
        raise ValidationError('Nie można przekazać zamkniętej pracy.')
    old=stage.assignment
    if not old or old.pk != expected_assignment_id or not old.is_current or not old.assigned_to_id:
        raise ValidationError('Przydział zmienił się lub nie ma wykonawcy. Odśwież stronę.')
    target=get_user_model().objects.get(pk=assigned_to_id)
    if target.pk==old.assigned_to_id:raise ValidationError('Wybierz inną osobę.')
    _require_eligible_assignee(target,old.role)
    _require_distinct_verifiers(list(current_assignment_queryset(text)),old.role,target.pk)
    from core.workflow_events import remember
    from texts.models import Text
    remember(Text,text,text._state.db or 'default')
    old.is_current=False;old.save(update_fields=['is_current'])
    number=(A.objects.filter(text=text,workflow_cycle=text.current_workflow_cycle,role=old.role).aggregate(n=Max('execution_number'))['n'] or 0)+1
    new=A.objects.create(text=text,workflow_cycle=text.current_workflow_cycle,role=old.role,assigned_to=target,execution_number=number,repetition=old.repetition)
    WorkflowHandoff.objects.create(text=text,stage=stage,previous_assignment=old,new_assignment=new,actor=user,original_started_at=stage.started_at,reason=reason)
    # Completed work stays with the previous person; unfinished returns follow the new assignment.
    S.objects.current_cycle().filter(text=text,assignment=old,is_completed=False).update(assignment=new)
    return new
