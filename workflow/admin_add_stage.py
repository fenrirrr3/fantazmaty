"""Add a missing workflow stage without rewriting completed work."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from core.edit_versions import version_of, bump
from core.services.texts import _require_eligible_assignee
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import STAGE_ROLES, _assign_role, ensure_verification_not_completed
from workflow.anthology_policy import require_working_anthology
from workflow.admin_status import set_admin_status


@transaction.atomic
def add_missing_stage(text_id, actor, version, *, kind, performer=None, started_at=None, ended_at=None, historical=False):
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
    ensure_verification_not_completed(text, kind)
    if type(historical) is not bool:
        raise ValidationError("Nieprawidłowy tryb uzupełnienia historii.")
    if historical and not performer:
        raise ValidationError("Zakończona praca historyczna wymaga wykonawcy.")
    if not historical and ended_at and (not started_at or not performer):
        raise ValidationError('Zakończony etap wymaga wykonawcy oraz obu dat.')
    if started_at and not performer:
        raise ValidationError('Rozpoczęty etap wymaga wykonawcy.')
    role = STAGE_ROLES[kind]
    if performer and not historical:
        try:
            _require_eligible_assignee(performer, role)
        except PermissionDenied as error:
            raise ValidationError(str(error) or 'Wybrana osoba nie może wykonywać tego etapu.') from error
    if (historical or ended_at) and role == A.Role.PROOFREADER_1 and A.objects.filter(text=text, assigned_to=performer, role__in=(A.Role.PROOFREADER_2, A.Role.PROOFREADER_3), stages__is_current=True, stages__is_completed=False).exists():
        raise ValidationError('Ta osoba ma już otwartą drugą lub trzecią korektę. Pierwszą korektę musi wykonać inna osoba.')
    if not ended_at and not historical:
        if current.exclude(stage_type__in=('ready', 'withdrawn')).filter(is_completed=False).exists():
            raise ValidationError('Istnieje otwarty etap. Zakończ go lub użyj cofnięcia statusu. Brakujący zakończony etap możesz uzupełnić z datami.')
        stage = set_admin_status(text.pk, kind, actor, version)
        if performer:
            stage.assignment = _assign_role(text, role, performer)
        stage.started_at = started_at
    else:
        # A completed missing checkpoint does not change the current status.
        if historical:
            assignment = A.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, role=role,
                                          is_current=True, assigned_to=performer).first()
            if assignment is None:
                number = (A.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, role=role).aggregate(n=Max('execution_number'))['n'] or 0) + 1
                assignment = A(text=text, workflow_cycle=text.current_workflow_cycle, role=role,
                    assigned_to=performer, is_current=False, execution_number=number)
                assignment.full_clean()
                assignment.save()
                # Historical work has no known assignment date. Do not invent today.
                A.objects.filter(pk=assignment.pk).update(assigned_at=None)
                assignment.assigned_at = None
        else:
            assignment = _assign_role(text, role, performer)
        iteration = (S.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind).aggregate(n=Max('iteration'))['n'] or 0) + 1
        stage = S(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind,
                  iteration=iteration, assignment=assignment, execution_number=assignment.execution_number,
                  started_at=started_at, ended_at=ended_at, is_completed=True, imported_completed=historical)
    from workflow.import_context import importing_completed
    token = importing_completed.set(historical)
    try:
        stage.full_clean()
        stage.save()
    finally:
        importing_completed.reset(token)
    bump('texts.text', text.pk, text._state.db or 'default')
    return stage
