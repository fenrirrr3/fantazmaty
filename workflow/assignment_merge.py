"""Grouping helpers for explicit administrator corrections of performers.

Automatic bulk merging has been retired. These helpers remain in use by
workflow.admin_performers when an administrator explicitly corrects a person.
"""
from django.db import models

from core.edit_versions import bump
from workflow.models import (WorkflowHandoff as H, WorkflowRepetition as R, WorkflowRoleAssignment as A,
                             WorkflowStage as S)
from workflow.services import STAGE_ROLES


def role_group(database, text, cycle, role):
    # Dodatkowe wykonania (wyk. 2, 3… dopisane w adminie) to osobna praca, nie duplikat przydziału roli.
    rows = list(A.objects.using(database).filter(text_id=text.pk, workflow_cycle=cycle, role=role,
                                                 is_additional=False).order_by('execution_number', 'pk'))
    if role not in STAGE_ROLES.values() or not rows:
        return None, ''
    ids = {row.pk for row in rows}
    stages = list(S.objects.using(database).filter(
        models.Q(assignment_id__in=ids) | models.Q(
            text_id=text.pk, workflow_cycle=cycle, assignment__isnull=True,
            repetition__isnull=True, is_skipped=False,
            stage_type__in=[kind for kind, mapped in STAGE_ROLES.items() if mapped == role],
        )).order_by('pk'))
    stage_ids = {stage.pk for stage in stages}
    if any(row.repetition_id for row in rows) or any(stage.repetition_id for stage in stages):
        return None, 'Jawne powtórzenie etapów.'
    for run in R.objects.using(database).filter(text_id=text.pk):
        if ids.intersection(run.previous_assignment_ids or []) or stage_ids.intersection(run.previous_stage_ids or []):
            return None, 'Przydział zapisany w historii powtórzeń.'
    if H.objects.using(database).filter(
            models.Q(previous_assignment_id__in=ids) | models.Q(new_assignment_id__in=ids)
            | models.Q(stage_id__in=stage_ids)).exists():
        return None, 'Zarejestrowane przekazanie pracy.'
    if any(stage.text_id != text.pk or stage.workflow_cycle != cycle
           or STAGE_ROLES.get(stage.stage_type) != role for stage in stages):
        return None, 'Powiązanie etapu z innym tekstem, przebiegiem lub rolą.'
    canonical = next((row for row in rows if row.is_current), rows[0])
    chosen = next((row for row in reversed(rows) if row.assigned_to_id is not None), canonical)
    notes = '\n\n'.join(dict.fromkeys(row.notes for row in rows if row.notes))
    return {
        'assignment': canonical, 'rows': rows, 'stages': stages,
        'merge_ids': ids - {canonical.pk}, 'chosen': chosen, 'notes': notes,
    }, ''


def write_group(database, plan, *, user_id, is_current):
    assignment = plan['assignment']
    ids = plan['merge_ids']
    A.objects.using(database).filter(pk__in=ids).update(is_current=False)
    A.objects.using(database).filter(pk=assignment.pk).update(
        assigned_to_id=user_id, is_current=is_current, notes=plan['notes'],
        assigned_at=assignment.assigned_at or plan['chosen'].assigned_at,
    )
    for stage in plan['stages']:
        if stage.assignment_id != assignment.pk:
            # Dates, iterations, completion and stage execution numbers stay intact.
            S.objects.using(database).filter(pk=stage.pk).update(assignment_id=assignment.pk)
            bump('workflow.workflowstage', stage.pk, database)
    A.objects.using(database).filter(pk__in=ids).delete()
    bump('workflow.workflowroleassignment', assignment.pk, database)
    bump('texts.text', assignment.text_id, database)
