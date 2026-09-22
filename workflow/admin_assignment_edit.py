"""Explicit superuser data corrections, not workflow transitions."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from texts.models import Text
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S, WorkflowHandoff, WorkflowRepetition
from workflow.import_context import importing_completed
from core.edit_versions import version_of, bump


def correct_assignment(pk, actor, version, *, action, performer=None):
    if not actor.is_active or not actor.is_superuser:
        raise PermissionDenied
    with transaction.atomic():
        text_id = A.objects.values_list('text_id', flat=True).get(pk=pk)
        text = Text.objects.select_for_update().get(pk=text_id)
        assignment = A.objects.select_for_update().get(pk=pk)
        if version_of(text) != version:
            raise ValidationError('Dane zmieniły się. Odśwież formularz.')
        stages = S.objects.filter(assignment=assignment)
        if assignment.repetition_id or stages.filter(repetition__isnull=False).exists() or any(
            assignment.pk in (run.previous_assignment_ids or [])
            for run in WorkflowRepetition.objects.filter(text=text)
        ):
            raise ValidationError('Przypisanie jest częścią historii powtórzeń. Nie można go poprawić tym formularzem.')
        if WorkflowHandoff.objects.filter(text=text).filter(
            Q(previous_assignment=assignment)
            | Q(new_assignment=assignment)
        ).exists():
            raise ValidationError('Przypisanie ma historię przekazania pracy. Nie można jej usunąć ani przepisać tym formularzem.')
        if action == 'delete':
            if stages.exists():
                raise ValidationError('Najpierw usuń powiązane etapy lub zmień ich wykonawcę. Przypisanie nadal jest używane.')
            assignment.delete()
        elif action in ('performer', 'clear'):
            if action == 'clear':
                performer = None
            if action == 'performer' and performer is None:
                raise ValidationError('Wybierz wykonawcę.')
            if assignment.assigned_to_id == (performer.pk if performer else None):
                return text.pk
            if performer and stages.filter(is_current=True, is_completed=False).exists():
                from core.services.texts import _require_eligible_assignee
                _require_eligible_assignee(performer, assignment.role)
            assignment.assigned_to = performer
            token = importing_completed.set(True)
            try:
                assignment.full_clean()
                # A correction preserves dates, including unknown imported dates.
                A.objects.filter(pk=pk).update(assigned_to=performer)
                if action == 'clear':
                    for stage in stages.filter(is_completed=False):
                        S.objects.filter(pk=stage.pk).update(started_at=None, ended_at=None)
                        bump('workflow.workflowstage', stage.pk, 'default')
            finally:
                importing_completed.reset(token)
            bump('workflow.workflowroleassignment', pk, 'default')
        else:
            raise ValidationError('Nieprawidłowa operacja.')
        bump('texts.text', text.pk, 'default')
        return text.pk
