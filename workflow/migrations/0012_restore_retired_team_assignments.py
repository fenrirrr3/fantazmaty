"""Restore team roles retired by a manual status correction, even without duplicates."""
from django.db import migrations, models


STAGE_ROLES = {
    'editing': 'editor', 'author_editing': 'editor', 'editor_control': 'editor',
    'first_verification': 'verifier_1', 'second_verification': 'verifier_2',
    'third_verification': 'verifier_3', 'editing_control': 'editing_coordinator',
    'coordinator_control': 'verification_coordinator',
    'first_proofreading': 'proofreader_1', 'second_proofreading': 'proofreader_2',
    'third_proofreading': 'proofreader_3', 'fourth_proofreading': 'proofreader_4',
    'styling': 'styling',
}


def restore_team_assignments(apps, database, *, apply=True):
    A = apps.get_model('workflow', 'WorkflowRoleAssignment')
    S = apps.get_model('workflow', 'WorkflowStage')
    R = apps.get_model('workflow', 'WorkflowRepetition')
    H = apps.get_model('workflow', 'WorkflowHandoff')
    T = apps.get_model('texts', 'Text')
    Revision = apps.get_model('core', 'EditRevision')
    assignments = A.objects.using(database)
    stages = S.objects.using(database)
    report = {'restored': 0, 'merged': 0, 'skipped': [], 'assignments': []}

    def bump(label, pk):
        revisions = Revision.objects.using(database)
        if not revisions.filter(model_label=label, object_id=pk).update(version=models.F('version') + 1):
            revisions.create(model_label=label, object_id=pk, version=1)

    text_ids = assignments.filter(
        workflow_cycle=models.F('text__current_workflow_cycle'),
        is_current=False, assigned_to__isnull=False, role__in=set(STAGE_ROLES.values()),
    ).order_by().values_list('text_id', flat=True).distinct()
    for text in T.objects.using(database).select_for_update().filter(pk__in=text_ids).iterator():
        current_stages = stages.filter(text_id=text.pk, workflow_cycle=text.current_workflow_cycle,
                                       is_current=True)
        # Closed texts and canceled queues have no live team to reconstruct.
        if current_stages.filter(stage_type__in=('ready', 'withdrawn')).exists() or not current_stages.filter(
                is_completed=False, is_released=True, stage_type__in=STAGE_ROLES).exists():
            continue
        rows = list(assignments.select_for_update().filter(
            text_id=text.pk, workflow_cycle=text.current_workflow_cycle,
            role__in=set(STAGE_ROLES.values()),
        ).order_by('role', 'execution_number', 'pk'))
        grouped = {}
        for row in rows:
            grouped.setdefault(row.role, []).append(row)
        protected_assignments, protected_stages = set(), set()
        for run in R.objects.using(database).filter(text_id=text.pk):
            protected_assignments.update(run.previous_assignment_ids or [])
            protected_stages.update(run.previous_stage_ids or [])
        for old, new, stage_id in H.objects.using(database).filter(text_id=text.pk).values_list(
                'previous_assignment_id', 'new_assignment_id', 'stage_id'):
            protected_assignments.update((old, new))
            protected_stages.add(stage_id)
        verifier_users = {row.role: row.assigned_to_id for row in rows
                          if row.is_current and row.role in ('verifier_1', 'verifier_2')}

        for role, history in grouped.items():
            current = next((row for row in history if row.is_current), None)
            if current is not None and current.assigned_to_id is not None:
                continue
            canonical = next((row for row in reversed(history) if row.assigned_to_id is not None), None)
            if canonical is None or canonical.is_current:
                continue
            placeholders = history[history.index(canonical) + 1:]
            previous = list(stages.select_for_update().filter(assignment_id=canonical.pk))
            linked = list(stages.select_for_update().filter(assignment_id__in=[row.pk for row in placeholders]))
            valid = (
                (current is None or current.pk in {row.pk for row in placeholders})
                and canonical.repetition_id is None and canonical.pk not in protected_assignments
                and bool(previous) and any(stage.is_completed and not stage.is_skipped for stage in previous)
                and all(not stage.is_current and stage.repetition_id is None and stage.pk not in protected_stages
                        and stage.text_id == text.pk and stage.workflow_cycle == text.current_workflow_cycle
                        and STAGE_ROLES.get(stage.stage_type) == role for stage in previous)
                and all(row.assigned_to_id is None and row.assigned_at is None and not row.notes
                        and row.repetition_id is None and row.pk not in protected_assignments
                        for row in placeholders)
                and all(stage.repetition_id is None and stage.pk not in protected_stages
                        and stage.text_id == text.pk and stage.workflow_cycle == text.current_workflow_cycle
                        and STAGE_ROLES.get(stage.stage_type) == role and stage.started_at is None
                        and stage.ended_at is None and not stage.is_completed and not stage.is_skipped
                        and not stage.imported_completed for stage in linked)
            )
            if role in ('verifier_1', 'verifier_2'):
                other_role = 'verifier_2' if role == 'verifier_1' else 'verifier_1'
                valid = valid and verifier_users.get(other_role) != canonical.assigned_to_id
            if not valid:
                report['skipped'].append((text.pk, role, canonical.pk))
                continue
            if apply:
                ids = [row.pk for row in placeholders]
                assignments.filter(pk__in=ids).update(is_current=False)
                stages.filter(pk__in=[stage.pk for stage in linked]).update(
                    assignment_id=canonical.pk, execution_number=canonical.execution_number)
                assignments.filter(pk=canonical.pk).update(is_current=True)
                assignments.filter(pk__in=ids).delete()
                for stage in linked:
                    bump('workflow.workflowstage', stage.pk)
                bump('workflow.workflowroleassignment', canonical.pk)
                bump('texts.text', text.pk)
            report['restored'] += 1
            report['merged'] += len(placeholders)
            report['assignments'].append((text.pk, text.title, role, canonical.pk, canonical.assigned_to_id))
            if role in ('verifier_1', 'verifier_2'):
                verifier_users[role] = canonical.assigned_to_id
    return report


def restore(apps, schema_editor):
    report = restore_team_assignments(apps, schema_editor.connection.alias)
    print(f"\nZespół tekstu: przywrócono {report['restored']} przypisań, scalono {report['merged']} pustych rekordów.")
    for text_id, title, role, assignment_id, user_id in report['assignments']:
        print(f'Tekst {text_id} „{title}”: {role}, przydział {assignment_id}, osoba {user_id}.')
    for text_id, role, assignment_id in report['skipped']:
        print(f'Bez zmian, do kontroli: tekst {text_id}, rola {role}, przydział {assignment_id}.')


class Migration(migrations.Migration):
    dependencies = [('workflow', '0011_merge_status_assignments')]
    operations = [migrations.RunPython(restore, migrations.RunPython.noop, atomic=True)]
