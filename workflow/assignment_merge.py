"""Grouping helpers for explicit administrator corrections of performers.

Automatic bulk merging has been retired. These helpers remain in use by
workflow.admin_performers when an administrator explicitly corrects a person.
"""
from django.db import models


STAGE_ROLES = {
    'editing': 'editor', 'author_editing': 'editor', 'editor_control': 'editor',
    'first_verification': 'verifier_1', 'second_verification': 'verifier_2',
    'third_verification': 'verifier_3', 'editing_control': 'editing_coordinator',
    'coordinator_control': 'verification_coordinator',
    'first_proofreading': 'proofreader_1', 'second_proofreading': 'proofreader_2',
    'third_proofreading': 'proofreader_3', 'fourth_proofreading': 'proofreader_4',
    'styling': 'styling',
}


def role_group(apps, database, text, cycle, role):
    A = apps.get_model('workflow', 'WorkflowRoleAssignment')
    S = apps.get_model('workflow', 'WorkflowStage')
    R = apps.get_model('workflow', 'WorkflowRepetition')
    H = apps.get_model('workflow', 'WorkflowHandoff')
    rows = list(A.objects.using(database).filter(text_id=text.pk, workflow_cycle=cycle, role=role)
                .order_by('execution_number', 'pk'))
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


def write_group(apps, database, plan, *, user_id, is_current):
    A = apps.get_model('workflow', 'WorkflowRoleAssignment')
    S = apps.get_model('workflow', 'WorkflowStage')
    Revision = apps.get_model('core', 'EditRevision')
    assignment = plan['assignment']
    ids = plan['merge_ids']

    def bump(label, pk):
        revisions = Revision.objects.using(database)
        if not revisions.filter(model_label=label, object_id=pk).update(version=models.F('version') + 1):
            revisions.create(model_label=label, object_id=pk, version=1)

    A.objects.using(database).filter(pk__in=ids).update(is_current=False)
    A.objects.using(database).filter(pk=assignment.pk).update(
        assigned_to_id=user_id, is_current=is_current, notes=plan['notes'],
        assigned_at=assignment.assigned_at or plan['chosen'].assigned_at,
    )
    for stage in plan['stages']:
        if stage.assignment_id != assignment.pk:
            # Dates, iterations, completion and stage execution numbers stay intact.
            S.objects.using(database).filter(pk=stage.pk).update(assignment_id=assignment.pk)
            bump('workflow.workflowstage', stage.pk)
    A.objects.using(database).filter(pk__in=ids).delete()
    bump('workflow.workflowroleassignment', assignment.pk)
    bump('texts.text', assignment.text_id)
