"""Explicit superuser data corrections, not workflow transitions."""
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from workflow.admin_status import admin_correction
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S, WorkflowHandoff, WorkflowRepetition
from workflow.import_context import importing_completed
from core.edit_versions import bump


def correct_assignment(pk, actor, version, *, action, performer=None):
    with admin_correction(actor, version, of=(A, pk), history=False) as text:
        assignment = A.objects.select_for_update().get(pk=pk)
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
                # Zakończona praca zachowuje wykonawcę – inaczej kolejna osoba przejęłaby cudzy dorobek.
                if stages.filter(is_completed=True).exists():
                    raise ValidationError('Przypisanie ma zakończone etapy. Nie można odłączyć osoby – '
                                          'zakończona praca musi zachować wykonawcę. Zmień wykonawcę formularzem korekty.')
            if action == 'performer' and performer is None:
                raise ValidationError('Wybierz wykonawcę.')
            if action == 'performer' and stages.exists():
                from workflow.admin_performers import correct_stage_performers
                correct_stage_performers(text.pk, {stages.order_by('pk').first().pk: performer}, actor, version)
                return text.pk
            if assignment.assigned_to_id == (performer.pk if performer else None):
                return text.pk
            from workflow.admin_performers import validate_proofreader_plan
            validate_proofreader_plan(text, [{'assignment': assignment, 'performer': performer, 'stages': list(stages)}])
            assignment.assigned_to = performer
            token = importing_completed.set(True)
            try:
                assignment.full_clean()
                # A correction preserves dates, including unknown imported dates.
                A.objects.filter(pk=pk).update(assigned_to=performer)
                if action == 'clear':
                    for stage in stages.filter(is_completed=False):
                        S.objects.filter(pk=stage.pk).update(started_at=None, ended_at=None, waiting_reset_at=timezone.localdate())
                        bump('workflow.workflowstage', stage.pk, 'default')
            finally:
                importing_completed.reset(token)
            bump('workflow.workflowroleassignment', pk, 'default')
        else:
            raise ValidationError('Nieprawidłowa operacja.')
        bump('texts.text', text.pk, 'default')
        return text.pk
