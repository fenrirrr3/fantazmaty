"""Explicit correction of a reservation falsely recorded as completed W1.

Unknown dates alone never invalidate historical work. Call only after the
operator confirms that the selected first verification has not been performed.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from texts.models import Text
from workflow.anthology_policy import require_working_anthology
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowHandoff, WorkflowRepetition


def reservation_candidate(stage):
    """Display an explicit correction option; the mutation checks the full text."""
    return (stage.stage_type == S.StageType.FIRST_VERIFICATION
            and stage.is_completed and not stage.is_skipped and not stage.repetition_id
            and stage.started_at is None and stage.ended_at is None)


def restore_first_verification_reservation(stage_id, *, apply=False, using='default'):
    with transaction.atomic(using=using):
        text_id = S.objects.using(using).values_list('text_id', flat=True).get(pk=stage_id)
        text = Text.objects.using(using).select_for_update().get(pk=text_id)
        stages = list(S.objects.using(using).select_for_update().filter(
            text=text, workflow_cycle=text.current_workflow_cycle,
        ).select_related('assignment').order_by('pk'))
        stage = next((s for s in stages if s.pk == stage_id), None)
        if stage is None or stage.stage_type != S.StageType.FIRST_VERIFICATION:
            raise ValidationError('Wskaż pierwszą weryfikację z aktualnego przebiegu tekstu.')
        if (not stage.is_completed and not stage.imported_completed and stage.is_current
                and stage.is_released and stage.started_at is None and stage.ended_at is None):
            return dict(changed=False, text_id=text.pk, title=text.title, stage_id=stage.pk)
        if not reservation_candidate(stage):
            raise ValidationError('Korekta dotyczy wyłącznie W1 błędnie zakończonej bez obu dat. Pracy z datami lub powtórzenia nie zmieniono.')
        require_working_anthology(text)
        if any(s.stage_type not in (S.StageType.READY_FOR_EDITING, S.StageType.EDITING,
                                    S.StageType.FIRST_VERIFICATION) for s in stages):
            raise ValidationError('Tekst ma dalsze etapy lub historię po W1. Nie można automatycznie przywrócić tej rezerwacji.')
        if len([s for s in stages if s.stage_type == S.StageType.FIRST_VERIFICATION]) != 1:
            raise ValidationError('Istnieje więcej niż jedno wykonanie W1. Najpierw sprawdź historię; niczego nie zmieniono.')
        opened = [s for s in stages if s.is_current and s.is_released
                  and not s.is_completed and s.ended_at is None]
        if (len(opened) != 1 or opened[0].stage_type != S.StageType.EDITING
                or not opened[0].started_at or opened[0].started_at > timezone.localdate()):
            raise ValidationError('Przywrócenie oczekiwania na przekazanie wymaga jednej aktywnej redakcji.')
        assignment = stage.assignment
        if (assignment is None or assignment.role != A.Role.VERIFIER_1
                or assignment.text_id != text.pk or assignment.workflow_cycle != text.current_workflow_cycle
                or assignment.assigned_to_id is None or assignment.repetition_id):
            raise ValidationError('W1 nie ma prawidłowo przypisanej weryfikatorki lub weryfikatora.')
        current = list(A.objects.using(using).select_for_update().filter(
            text=text, workflow_cycle=text.current_workflow_cycle, role=A.Role.VERIFIER_1, is_current=True,
        ))
        if any(a.pk != assignment.pk for a in current):
            raise ValidationError('Rola W1 ma inne aktualne przypisanie. Najpierw popraw przypisania w panelu admina.')
        if S.objects.using(using).filter(assignment_id=assignment.pk).exclude(pk=stage.pk).exists():
            raise ValidationError('Przypisanie W1 ma inne wykonania. Nie zmieniono historii.')
        if WorkflowHandoff.objects.using(using).filter(
                Q(stage_id=stage.pk) | Q(previous_assignment_id=assignment.pk)
                | Q(new_assignment_id=assignment.pk)).exists():
            raise ValidationError('W1 ma historię przekazania pracy. Ta korekta nie może jej nadpisać.')
        for run in WorkflowRepetition.objects.using(using).filter(text=text):
            if (not run.completed_at and not run.canceled_at
                    or stage.pk in (run.previous_stage_ids or [])
                    or assignment.pk in (run.previous_assignment_ids or [])):
                raise ValidationError('W1 lub tekst są częścią powtórzenia etapów. Najpierw sprawdź kolejkę.')
        from workflow.admin_performers import require_eligible_correction
        require_eligible_correction(assignment.assigned_to, A.Role.VERIFIER_1)
        before = dict(is_completed=stage.is_completed, is_current=stage.is_current,
                      is_released=stage.is_released, imported_completed=stage.imported_completed,
                      assignment_current=assignment.is_current)
        stage.is_completed = False
        stage.imported_completed = False
        stage.is_current = True
        stage.is_released = True
        assignment.is_current = True
        token = importing_completed.set(True)
        try:
            assignment.full_clean()
            stage.full_clean()
            if apply:
                from core.workflow_events import remember
                remember(Text, text, using)
                if not before['assignment_current']:
                    assignment.save(update_fields=['is_current'], using=using)
                stage.save(update_fields=['is_completed', 'imported_completed', 'is_current', 'is_released'], using=using)
        finally:
            importing_completed.reset(token)
        return dict(changed=True, applied=apply, text_id=text.pk, title=text.title,
                    stage_id=stage.pk, assignment_id=assignment.pk,
                    user_id=assignment.assigned_to_id, before=before,
                    after=dict(is_completed=False, is_current=True, is_released=True,
                               imported_completed=False, assignment_current=True))
