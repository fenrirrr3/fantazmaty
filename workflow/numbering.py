"""Wspólna numeracja wykonań i podejść w workflow.

Jedno miejsce dla trzech liczników, które wcześniej były kopiowane po serwisach:

- numer wykonania przydziału roli (``WorkflowRoleAssignment.execution_number``),
- numer wykonania etapu (``WorkflowStage.execution_number``),
- numer podejścia etapu (``WorkflowStage.iteration``).

Każdy licznik jest liczony w obrębie tekstu i przebiegu (``workflow_cycle``);
domyślnie bieżącego przebiegu tekstu. Numer wykonania etapu ma być zgodny
z numerem wykonania przydziału, do którego etap jest podpięty – stąd
``stage_execution_for``.
"""
from django.db.models import Max

from workflow.models import WorkflowRoleAssignment, WorkflowStage


def _cycle(text, cycle):
    return text.current_workflow_cycle if cycle is None else cycle


def _next(queryset, field):
    return (queryset.aggregate(maximum=Max(field))["maximum"] or 0) + 1


def next_assignment_execution(text, role, *, cycle=None):
    """Następny numer wykonania przydziału roli w tekście i przebiegu.

    Liczy wszystkie przydziały roli w przebiegu (także nieaktualne), bo numer
    musi być unikalny w obrębie tekst + przebieg + rola.
    """
    return _next(
        WorkflowRoleAssignment.objects.using(text._state.db).filter(
            text_id=text.pk, workflow_cycle=_cycle(text, cycle), role=role,
        ),
        "execution_number",
    )


def next_stage_execution(text, stage_type, *, cycle=None):
    """Następny numer wykonania etapu danego rodzaju w tekście i przebiegu.

    Używane tylko tam, gdzie etap nie ma przydziału roli, z którego można
    przepisać numer (``stage_execution_for``).
    """
    return _next(
        WorkflowStage.objects.using(text._state.db).filter(
            text_id=text.pk, workflow_cycle=_cycle(text, cycle), stage_type=stage_type,
        ),
        "execution_number",
    )


def next_stage_iteration(text, stage_type, *, cycle=None):
    """Następny numer podejścia etapu danego rodzaju w tekście i przebiegu.

    Liczy także etapy nieaktualne, bo ``iteration`` jest unikalne w obrębie
    tekst + przebieg + rodzaj etapu.
    """
    return _next(
        WorkflowStage.objects.using(text._state.db).filter(
            text_id=text.pk, workflow_cycle=_cycle(text, cycle), stage_type=stage_type,
        ),
        "iteration",
    )


def stage_execution_for(assignment):
    """Numer wykonania etapu wynikający z przydziału; bez przydziału – 1."""
    return assignment.execution_number if assignment is not None else 1
