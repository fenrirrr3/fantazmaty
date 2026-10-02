from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowRepetition, WorkflowHandoff
from workflow.anthology_policy import require_working_anthology
from core.edit_versions import version_of


def _reopen_stage(text, stage):
    """Correct the existing execution, retiring only empty continuations."""
    from django.utils import timezone
    from workflow.state import ORDER
    from workflow.import_context import importing_completed

    if (not stage.is_completed or not stage.is_current or not stage.is_released
            or stage.workflow_cycle != text.current_workflow_cycle
            or stage.stage_type not in ORDER
            or stage.stage_type in ('ready_for_editing', 'ready', 'withdrawn')):
        raise ValidationError('Można cofnąć zakończenie tylko aktualnego, zakończonego etapu pracy.')
    if stage.repetition_id and stage.repetition.canceled_at:
        raise ValidationError('Etap należy do odwołanej kolejki powtórzeń.')
    if stage.is_skipped:
        raise ValidationError('Ten etap został pominięty, a nie wykonany. Aby go rozpocząć, użyj resetowania etapów.')
    if not stage.started_at:
        raise ValidationError('Etap nie ma daty rozpoczęcia. Najpierw uzupełnij ją w panelu admina.')
    if stage.started_at > timezone.localdate():
        raise ValidationError('Trwający etap nie może mieć przyszłej daty rozpoczęcia.')
    if stage.assignment_id and not stage.assignment.is_current:
        raise ValidationError('To wykonanie ma już zastąpionego wykonawcę. Popraw aktualny etap.')
    if text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).exists():
        raise ValidationError('Najpierw zakończ lub odwołaj kolejkę powtórzeń.')

    others = list(S.objects.select_for_update().current_cycle().select_related('assignment').filter(text=text).exclude(pk=stage.pk))
    if any(other.stage_type == 'withdrawn' for other in others):
        raise ValidationError('Tekst jest wycofany. Najpierw przywróć go do opracowania.')
    for other in others:
        if other.stage_type in ('ready', 'withdrawn'):
            continue
        later = (ORDER.get(other.stage_type, -1) > ORDER[stage.stage_type]
                 or other.stage_type == stage.stage_type and other.iteration > stage.iteration
                 or stage.stage_type in ('first_verification', 'author_editing', 'second_verification')
                 and other.stage_type == 'editing' and other.pk > stage.pk)
        if stage.repetition_id:
            # Unselected completed work stays untouched when correcting the
            # final step of an already finished selective repetition.
            later = (other.repetition_id == stage.repetition_id
                     and other.queue_position > stage.queue_position)
        if (other.is_completed and later
                or not other.is_completed and (other.started_at or other.ended_at
                    or other.assignment_id and other.assignment.assigned_to_id)):
            raise ValidationError('Dalsza praca została już rozpoczęta, przypisana lub zakończona. Najpierw popraw dalsze etapy w panelu admina.')

    from core.workflow_events import remember
    remember(Text, text, text._state.db or 'default')
    if stage.repetition_id:
        stage.repetition.completed_at = None
        stage.repetition.save(update_fields=['completed_at'])
    for other in others:
        if other.stage_type == 'ready' or not other.is_completed:
            other.is_current = False
            other.is_released = False
            other.save(update_fields=['is_current', 'is_released'])

    stage.ended_at = None
    stage.is_completed = False
    stage.imported_completed = False
    # Imported work loses the exception for unknown dates once it is ongoing.
    token = importing_completed.set(True)
    try:
        stage.full_clean()
        stage.save(update_fields=['ended_at', 'is_completed', 'imported_completed'])
    finally:
        importing_completed.reset(token)
    return stage


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
        if action == 'reopen':
            return _reopen_stage(text, stage)
        if action=='delete':
            snapshots = list(WorkflowRepetition.objects.filter(text=text))
            if any(stage.pk in (run.previous_stage_ids or []) for run in snapshots):
                raise ValidationError('Etap jest zapisany w historii powtórzenia. Nie można usunąć go tym formularzem.')
            opened=stage.is_current and stage.is_released and not stage.is_completed
            aid=stage.assignment_id
            stage.delete()
            if aid and not S.objects.filter(assignment_id=aid).exists():
                # An orphan must not reserve a role after removing its only work.
                from django.db.models import Q
                protected_assignment = any(aid in (run.previous_assignment_ids or []) for run in snapshots)
                protected_assignment = protected_assignment or WorkflowHandoff.objects.filter(Q(previous_assignment_id=aid) | Q(new_assignment_id=aid)).exists()
                if not protected_assignment:
                    A.objects.filter(pk=aid).delete()
            if opened and replacement:
                from workflow.admin_status import set_admin_status
                set_admin_status(text.pk,replacement,actor,version_of(text))
            return
        if action!='performer' or not performer:
            raise ValidationError('Wybierz wykonawcę.')
        from workflow.admin_performers import correct_stage_performers
        correct_stage_performers(text.pk, {stage.pk: performer}, actor, version)
