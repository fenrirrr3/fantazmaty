"""One ordinary role assignment per text/cycle; stage history remains intact."""
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


def merge_duplicates(apps, database, *, apply=True, text_id=None):
    A = apps.get_model('workflow', 'WorkflowRoleAssignment')
    T = apps.get_model('texts', 'Text')
    S = apps.get_model('workflow', 'WorkflowStage')
    query = A.objects.using(database).filter(role__in=set(STAGE_ROLES.values()))
    if text_id is not None:
        query = query.filter(text_id=text_id)
    groups = list(query.order_by().values('text_id', 'workflow_cycle', 'role')
                  .annotate(total=models.Count('pk')).filter(total__gt=1)
                  .order_by('text_id', 'workflow_cycle', 'role'))
    report = {'merged': 0, 'groups': [], 'skipped': []}
    verifier_users = {}
    for group in groups:
        text = T.objects.using(database).select_for_update().get(pk=group['text_id'])
        cycle, role = group['workflow_cycle'], group['role']
        plan, reason = role_group(apps, database, text, cycle, role)
        if plan is None:
            report['skipped'].append((text.pk, role, reason))
            continue
        user_id = plan['chosen'].assigned_to_id
        active_text = S.objects.using(database).filter(
            text_id=text.pk, workflow_cycle=cycle, is_current=True, is_released=True,
            is_completed=False, stage_type__in=STAGE_ROLES,
        ).exists()
        current = cycle == text.current_workflow_cycle and (
            any(row.is_current for row in plan['rows']) or active_text and user_id is not None)
        if current and user_id is not None and role in ('verifier_1', 'verifier_2'):
            opposite = 'verifier_2' if role == 'verifier_1' else 'verifier_1'
            key = (text.pk, cycle, opposite)
            if key not in verifier_users:
                verifier_users[key] = A.objects.using(database).filter(
                    text_id=text.pk, workflow_cycle=cycle, role=opposite, is_current=True,
                ).values_list('assigned_to_id', flat=True).first()
            if verifier_users[key] == user_id:
                report['skipped'].append((text.pk, role, 'Ta sama osoba w obu weryfikacjach.'))
                continue
        if apply:
            write_group(apps, database, plan, user_id=user_id, is_current=current)
        if current:
            verifier_users[(text.pk, cycle, role)] = user_id
        report['merged'] += len(plan['merge_ids'])
        report['groups'].append((text.pk, text.title, cycle, role, plan['assignment'].pk, user_id))
    return report
