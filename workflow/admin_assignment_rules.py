"""Admin edits must not rewrite who performed recorded work."""
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import connection
from texts.models import Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage


def has_recorded_work(assignment):
    if connection.in_atomic_block:
        Text.objects.select_for_update().get(pk=assignment.text_id)
    from workflow.catalog import all_stage_roles
    kinds = [kind for kind, role in all_stage_roles().items() if role == assignment.role]
    return WorkflowStage.objects.filter(text_id=assignment.text_id, workflow_cycle=assignment.workflow_cycle, stage_type__in=kinds).filter(Q(started_at__isnull=False) | Q(ended_at__isnull=False) | Q(is_completed=True)).exists()


def protect_assignment(instance, data, deleting=False):
    if not instance.pk:
        return
    previous = WorkflowRoleAssignment.objects.get(pk=instance.pk)
    changed = deleting or any(data.get(field, getattr(previous, field)) != getattr(previous, field) for field in ('text', 'workflow_cycle', 'role', 'assigned_to'))
    if changed and has_recorded_work(previous):
        raise ValidationError("Ten przydział dokumentuje rozpoczętą lub zakończoną pracę. Nie można zmienić wykonawcy, roli ani przebiegu lub usunąć przydziału. Zastępstwo rozpocznij w nowym cyklu; poprzednia praca pozostanie w historii.")
