"""Explicit status correction, preserving recorded work and its performers."""
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.numbering import (next_assignment_execution, next_stage_execution, next_stage_iteration,
    stage_execution_for)
from workflow.catalog import active_stage_choices, require_stage_allowed
from workflow.services import STAGE_ROLES, ensure_verification_not_completed
from workflow.anthology_policy import require_working_anthology
from core.edit_versions import version_of


STALE_FORM = 'Dane zmieniły się. Odśwież formularz.'


@contextmanager
def admin_correction(actor, version, *, text_id=None, of=None, history=True, working_anthology=True):
    """Wspólny początek korekty w adminie; zwraca zablokowany tekst.

    Kolejno: tylko aktywny superuser, wpis do dziennika jako korekta historii (history), transakcja,
    blokada tekstu (podanego wprost albo przez `of` = (model etapu lub przypisania, pk)), aktualna
    wersja formularza i antologia w przygotowaniu (working_anthology).
    """
    if not actor.is_active or not actor.is_superuser:
        raise PermissionDenied
    if history:
        from core.workflow_events import history_correction
        history_correction()
    with transaction.atomic():
        if of is not None:
            model, pk = of
            text_id = model.objects.values_list('text_id', flat=True).get(pk=pk)
        text = Text.objects.select_for_update().get(pk=text_id)
        if version_of(text) != version:
            raise ValidationError(STALE_FORM)
        if working_anthology:
            require_working_anthology(text)
        yield text


def save_undated_assignment(assignment):
    """Zapisuje nowy przydział z korekty w adminie bez daty przypisania – korekta nie wymyśla dzisiejszej daty."""
    assignment.save()
    A.objects.filter(pk=assignment.pk).update(assigned_at=None)
    assignment.assigned_at = None


def set_admin_status(text_id, kind, actor, expected_version):
    # Zmiana statusu jest zdarzeniem workflow (z powiadomieniami), nie korektą historii.
    with admin_correction(actor, expected_version, text_id=text_id, history=False) as text:
        order = [k for k, _ in active_stage_choices()]
        if kind not in order:
            raise ValidationError('Nieprawidłowy etap.')
        require_stage_allowed(text, kind)
        ensure_verification_not_completed(text, kind)
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
        target_role = STAGE_ROLES.get(kind)
        # A status correction preserves the whole team. Only an explicit
        # repetition or handoff creates a new assignment execution.
        assignment = None
        if target_role:
            assignment = A.objects.filter(
                text=text, workflow_cycle=text.current_workflow_cycle,
                role=target_role, is_current=True,
            ).first()
        if target_role and assignment is None:
            assignment = A(text=text, workflow_cycle=text.current_workflow_cycle, role=target_role,
                           execution_number=next_assignment_execution(text, target_role))
            assignment.full_clean()
            save_undated_assignment(assignment)
        # Etap z rolą dziedziczy numer przydziału; bez roli (np. „Gotowe”) liczy własne wykonania.
        execution = (stage_execution_for(assignment) if assignment is not None
                     else next_stage_execution(text, kind))
        stage = S(
            text=text,
            workflow_cycle=text.current_workflow_cycle,
            stage_type=kind,
            iteration=next_stage_iteration(text, kind),
            execution_number=execution,
            assignment=assignment,
        )
        stage.full_clean()
        stage.save()
        return stage
