from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.db.models import Max
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.catalog import all_stage_roles
from workflow.import_context import importing_completed
from workflow.anthology_policy import require_working_anthology
from core.edit_versions import version_of


def edit_stage(stage_id, actor, version, *, action, performer=None, replacement=None):
    if not actor.is_active or not actor.is_superuser:
        raise PermissionDenied
    with transaction.atomic():
        text_id=S.objects.values_list('text_id',flat=True).get(pk=stage_id)
        text=Text.objects.select_for_update().get(pk=text_id)
        stage=S.objects.select_for_update().select_related('assignment').get(pk=stage_id)
        if version_of(text)!=version:
            raise ValidationError('Dane zmieniły się. Odśwież formularz.')
        require_working_anthology(text)
        if stage.repetition_id and not stage.repetition.completed_at and not stage.repetition.canceled_at:
            raise ValidationError('Najpierw zakończ lub odwołaj kolejkę powtórzeń.')
        if action=='delete':
            opened=stage.is_current and stage.is_released and not stage.is_completed
            if opened and not replacement:
                raise ValidationError('Przy usunięciu bieżącego etapu wybierz zastępujący go status.')
            aid=stage.assignment_id
            stage.delete()
            if aid and not S.objects.filter(assignment_id=aid).exists():
                # An orphan must not reserve a role after removing its only work.
                A.objects.filter(pk=aid).delete()
            if opened:
                from workflow.admin_status import set_admin_status
                set_admin_status(text.pk,replacement,actor,version_of(text))
            return
        if action!='performer' or not performer:
            raise ValidationError('Wybierz wykonawcę.')
        role=all_stage_roles().get(stage.stage_type)
        if not role:
            raise ValidationError('Ten status nie jest wykonywanym etapem.')
        if not stage.is_completed:
            from core.services.texts import _require_eligible_assignee
            _require_eligible_assignee(performer,role)
        previous=stage.assignment
        if previous and previous.assigned_to_id==performer.pk:return
        shared=bool(previous and previous.stages.exclude(pk=stage.pk).exists())
        if shared and previous.stages.exclude(pk=stage.pk).filter(is_current=True,is_completed=False).exists():
            raise ValidationError('Przydział obejmuje też inne otwarte etapy. Najpierw uporządkuj ich statusy.')
        token=importing_completed.set(True)
        try:
            if previous and not shared:
                assignment=previous
            else:
                number=(A.objects.filter(text=text,workflow_cycle=stage.workflow_cycle,role=role).aggregate(n=Max('execution_number'))['n'] or 0)+1
                assignment=A(text=text,workflow_cycle=stage.workflow_cycle,role=role,execution_number=number,is_current=bool(stage.is_current and not stage.is_completed))
            if previous and shared and assignment.is_current and previous.is_current:
                A.objects.filter(pk=previous.pk).update(is_current=False)
            assignment.assigned_to=performer
            assignment.full_clean();assignment.save()
            if not previous or shared:A.objects.filter(pk=assignment.pk).update(assigned_at=None)
            stage.assignment=assignment
            stage.full_clean();stage.save(update_fields=['assignment'])
        finally:
            importing_completed.reset(token)
