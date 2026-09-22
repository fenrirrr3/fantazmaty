"""Explicit status correction, preserving recorded work and its performers."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.catalog import active_stage_choices
from workflow.services import STAGE_ROLES
from workflow.anthology_policy import require_working_anthology
from core.edit_versions import version_of


def set_admin_status(text_id, kind, actor, expected_version):
    if not actor.is_active or not actor.is_superuser:
        raise PermissionDenied('Tylko superuser może ręcznie zmienić status.')
    order = [k for k, _ in active_stage_choices()]
    if kind not in order:
        raise ValidationError('Nieprawidłowy etap.')
    with transaction.atomic():
        text = Text.objects.select_for_update().get(pk=text_id)
        if version_of(text) != expected_version:
            raise ValidationError('Tekst zmienił się. Odśwież stronę przed zmianą statusu.')
        require_working_anthology(text)
        if text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).exists():
            raise ValidationError('Najpierw zakończ lub odwołaj aktywną kolejkę powtórzeń.')
        stages = S.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle, is_current=True)
        opened = stages.filter(is_completed=False).exclude(stage_type__in=('ready','withdrawn'))
        if (opened.filter(started_at__isnull=False).exists() or
                opened.filter(assignment__assigned_to__isnull=False).exists()):
            raise ValidationError('Tekst ma rozpoczętą pracę lub rezerwację. Najpierw ją zakończ albo odwołaj.')
        from core.workflow_events import remember
        remember(Text, text, 'default')
        terminal = kind in ('ready','withdrawn')
        later = order[order.index(kind):] if not terminal else []
        # Preserve completed rows, but remove downstream work from the current path.
        opened.update(is_current=False, is_released=False)
        stages.filter(stage_type__in=list(set(later+['ready','withdrawn']))).update(is_current=False, is_released=False)
        roles = {STAGE_ROLES[k] for k in later if k in STAGE_ROLES}
        # Editor is also used in author exchange / editorial verification control.
        target_role = STAGE_ROLES.get(kind)
        if target_role != 'editor':
            roles.discard('editor')
        A.objects.filter(text=text,workflow_cycle=text.current_workflow_cycle,is_current=True,role__in=roles).update(is_current=False)
        assignment = None
        if target_role:
            number = (A.objects.filter(text=text,workflow_cycle=text.current_workflow_cycle,role=target_role).aggregate(n=Max('execution_number'))['n'] or 0)+1
            assignment = A(text=text,workflow_cycle=text.current_workflow_cycle,role=target_role,execution_number=number)
            assignment.full_clean();assignment.save()
            A.objects.filter(pk=assignment.pk).update(assigned_at=None)
        all_kind = S.objects.filter(text=text,workflow_cycle=text.current_workflow_cycle,stage_type=kind)
        iteration=(all_kind.aggregate(n=Max('iteration'))['n'] or 0)+1
        execution=(S.objects.filter(text=text,stage_type=kind).aggregate(n=Max('execution_number'))['n'] or 0)+1
        stage=S(text=text,workflow_cycle=text.current_workflow_cycle,stage_type=kind,iteration=iteration,execution_number=execution,assignment=assignment)
        stage.full_clean();stage.save()
        return stage
