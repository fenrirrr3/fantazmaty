"""Add a missing workflow stage without rewriting completed work."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from core.edit_versions import version_of, bump
from core.services.texts import _require_eligible_assignee
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import STAGE_ROLES, _assign_role
from workflow.anthology_policy import require_working_anthology
from workflow.admin_status import set_admin_status


@transaction.atomic
def add_missing_stage(text_id, actor, version, *, kind, performer=None, started_at=None, ended_at=None):
    if not actor.is_active or not actor.is_superuser:
        raise PermissionDenied
    text = Text.objects.select_for_update().get(pk=text_id)
    require_working_anthology(text)
    if version != version_of(text):
        raise ValidationError('Workflow zmienił się. Odśwież formularz.')
    from workflow.catalog import active_stage_choices
    if kind not in dict(active_stage_choices()) or kind not in STAGE_ROLES:
        raise ValidationError('Wybierz etap pracy. Status zmienia się osobnym formularzem.')
    if text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).exists():
        raise ValidationError('Najpierw zakończ lub odwołaj kolejkę powtórzeń.')
    current = S.objects.current_cycle().filter(text=text)
    if current.filter(stage_type=kind).exists():
        raise ValidationError('Ten etap już istnieje. Edytuj wykonawcę w tabeli albo użyj powtórzenia/cofnięcia.')
    if ended_at and (not started_at or not performer):
        raise ValidationError('Zakończony etap wymaga wykonawcy oraz obu dat.')
    if started_at and not performer:
        raise ValidationError('Rozpoczęty etap wymaga wykonawcy.')
    role = STAGE_ROLES[kind]
    if performer:
        _require_eligible_assignee(performer, role)
    if ended_at and role == A.Role.PROOFREADER_1 and A.objects.filter(text=text, assigned_to=performer, role__in=(A.Role.PROOFREADER_2, A.Role.PROOFREADER_3), stages__is_current=True, stages__is_completed=False).exists():
        raise ValidationError('Ta osoba ma już otwartą drugą lub trzecią korektę. Pierwszą korektę musi wykonać inna osoba.')
    if not ended_at:
        if current.exclude(stage_type__in=('ready', 'withdrawn')).filter(is_completed=False).exists():
            raise ValidationError('Istnieje otwarty etap. Zakończ go lub użyj cofnięcia statusu. Brakujący zakończony etap możesz uzupełnić z datami.')
        stage = set_admin_status(text.pk, kind, actor, version)
        if performer:
            stage.assignment = _assign_role(text, role, performer)
        stage.started_at = started_at
    else:
        # A completed missing checkpoint does not change the current status.
        assignment = _assign_role(text, role, performer)
        iteration = (S.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind).aggregate(n=Max('iteration'))['n'] or 0) + 1
        stage = S(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind,
                  iteration=iteration, assignment=assignment, execution_number=assignment.execution_number,
                  started_at=started_at, ended_at=ended_at, is_completed=True)
    stage.full_clean()
    stage.save()
    bump('texts.text', text.pk, text._state.db or 'default')
    return stage
