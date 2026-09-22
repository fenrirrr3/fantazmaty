from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowRepetition, WorkflowHandoff
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
            snapshots = list(WorkflowRepetition.objects.filter(text=text))
            if any(stage.pk in (run.previous_stage_ids or []) for run in snapshots):
                raise ValidationError('Etap jest zapisany w historii powtórzenia. Nie można usunąć go tym formularzem.')
            opened=stage.is_current and stage.is_released and not stage.is_completed
            if opened and not replacement:
                raise ValidationError('Przy usunięciu bieżącego etapu wybierz zastępujący go status.')
            aid=stage.assignment_id
            stage.delete()
            if aid and not S.objects.filter(assignment_id=aid).exists():
                # An orphan must not reserve a role after removing its only work.
                from django.db.models import Q
                protected_assignment = any(aid in (run.previous_assignment_ids or []) for run in snapshots)
                protected_assignment = protected_assignment or WorkflowHandoff.objects.filter(Q(previous_assignment_id=aid) | Q(new_assignment_id=aid)).exists()
                if not protected_assignment:
                    A.objects.filter(pk=aid).delete()
            if opened:
                from workflow.admin_status import set_admin_status
                set_admin_status(text.pk,replacement,actor,version_of(text))
            return
        if action!='performer' or not performer:
            raise ValidationError('Wybierz wykonawcę.')
        from workflow.admin_performers import correct_stage_performers
        correct_stage_performers(text.pk, {stage.pk: performer}, actor, version)
