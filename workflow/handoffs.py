from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db.models import Max
from django.utils import timezone
from core.permissions import require_superuser
from core.services.texts import _require_eligible_assignee, _require_distinct_verifiers
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowHandoff
from workflow.services import _locked_text_operation, current_assignment_queryset


@_locked_text_operation
def handoff_stage(text, user, *, stage_id, assigned_to_id, expected_assignment_id, reason):
    require_superuser(user)
    reason=(reason or '').strip()
    if not reason:
        raise ValidationError("Podaj powód przekazania pracy.")
    stage = S.objects.get(pk=stage_id, text=text, is_current=True, is_released=True)
    if (
        stage.is_completed
        or stage.stage_type in ("ready", "withdrawn")
        or S.objects.current_cycle()
        .filter(text=text, stage_type__in=("ready", "withdrawn"))
        .exists()
    ):
        raise ValidationError("Nie można przekazać zamkniętej pracy.")
    old = stage.assignment
    if not old or old.pk != expected_assignment_id or not old.is_current or not old.assigned_to_id:
        raise ValidationError("Przydział zmienił się lub nie ma wykonawcy. Odśwież stronę.")
    target = get_user_model().objects.filter(pk=assigned_to_id).first()
    if target is None:
        raise ValidationError("Wybrane konto już nie istnieje. Odśwież formularz.")
    if target.pk == old.assigned_to_id:
        raise ValidationError("Wybierz inną osobę.")
    _require_eligible_assignee(target, old.role)
    from workflow.availability import ensure_distinct_proofreader

    ensure_distinct_proofreader(text, old.role, target)
    _require_distinct_verifiers(list(current_assignment_queryset(text)), old.role, target.pk)
    from core.workflow_events import remember
    from texts.models import Text

    remember(Text, text, text._state.db or "default")
    old.is_current = False
    old.save(update_fields=["is_current"])
    number = (
        A.objects.filter(
            text=text, workflow_cycle=text.current_workflow_cycle, role=old.role
        ).aggregate(n=Max("execution_number"))["n"]
        or 0
    ) + 1
    new = A.objects.create(
        text=text,
        workflow_cycle=text.current_workflow_cycle,
        role=old.role,
        assigned_to=target,
        execution_number=number,
        repetition=old.repetition,
    )
    WorkflowHandoff.objects.create(
        text=text,
        stage=stage,
        previous_assignment=old,
        new_assignment=new,
        actor=user,
        original_started_at=stage.started_at,
        reason=reason,
    )
    # Completed work stays with the previous person; unfinished returns follow the new assignment.
    pending = S.objects.current_cycle().filter(text=text, assignment=old, is_completed=False)
    pending.update(waiting_reset_at=timezone.localdate())
    # Waiting for the author continues regardless of who takes over editing.
    pending.filter(stage_type=S.StageType.AUTHOR_EDITING).update(assignment=new)
    pending.exclude(stage_type=S.StageType.AUTHOR_EDITING).update(assignment=new, started_at=None)
    return new


def eligible_handoff_users(stage):
    from django.db.models import Q
    from django.utils import timezone
    from workflow.availability import eligible_role_users

    users = (
        get_user_model().objects.filter(is_active=True).order_by("last_name", "first_name", "pk")
    )
    if (
        stage.is_completed
        or not stage.is_current
        or not stage.is_released
        or not stage.assignment_id
        or not stage.assignment.assigned_to_id
    ):
        return users.none()
    role = stage.assignment.role
    now = timezone.now()
    users = eligible_role_users(role).exclude(pk=stage.assignment.assigned_to_id)
    if role in (A.Role.PROOFREADER_2, A.Role.PROOFREADER_3):
        first = A.objects.filter(text=stage.text, role=A.Role.PROOFREADER_1).filter(
            Q(stages__is_completed=True)
            | Q(stages__started_at__lte=timezone.localdate(now))
            | Q(handoffs_from__isnull=False)
        )
        users = users.exclude(pk__in=first.exclude(assigned_to=None).values("assigned_to_id"))
    opposite = {A.Role.VERIFIER_1: A.Role.VERIFIER_2, A.Role.VERIFIER_2: A.Role.VERIFIER_1}.get(
        role
    )
    if opposite:
        users = users.exclude(
            pk__in=current_assignment_queryset(stage.text)
            .filter(role=opposite)
            .exclude(assigned_to=None)
            .values("assigned_to_id")
        )
    return users.distinct()
