"""Merge empty assignment executions created by the old manual-status action."""
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


def repair_status_assignments(apps, database, *, apply=True):
    """Preserve real work and IDs; skip anything without an unambiguous origin."""
    A = apps.get_model('workflow', 'WorkflowRoleAssignment')
    S = apps.get_model('workflow', 'WorkflowStage')
    R = apps.get_model('workflow', 'WorkflowRepetition')
    H = apps.get_model('workflow', 'WorkflowHandoff')
    T = apps.get_model('texts', 'Text')
    Revision = apps.get_model('core', 'EditRevision')
    assignments = A.objects.using(database)
    stages = S.objects.using(database)
    report = {'merged': 0, 'restored': 0, 'skipped': []}

    def bump(label, pk):
        revisions = Revision.objects.using(database)
        if not revisions.filter(model_label=label, object_id=pk).update(version=models.F('version') + 1):
            revisions.create(model_label=label, object_id=pk, version=1)

    text_ids = assignments.filter(
        workflow_cycle=models.F('text__current_workflow_cycle'),
        role__in=set(STAGE_ROLES.values()),
    ).values_list('text_id', flat=True).distinct()
    for text in T.objects.using(database).select_for_update().filter(pk__in=text_ids).iterator():
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
            latest = history[-1]
            if not latest.is_current or latest.assigned_to_id is not None or latest.execution_number < 2:
                continue
            canonical = next((row for row in reversed(history[:-1])
                              if row.assigned_to_id is not None), None)
            if canonical is None:
                continue
            placeholders = history[history.index(canonical) + 1:]
            linked = list(stages.select_for_update().filter(
                assignment_id__in=[row.pk for row in placeholders]).order_by('pk'))
            previous = list(stages.filter(assignment_id=canonical.pk))
            valid = (
                not canonical.is_current and canonical.repetition_id is None
                and canonical.pk not in protected_assignments
                and bool(previous)
                and all(stage.repetition_id is None and stage.text_id == text.pk
                        and stage.workflow_cycle == text.current_workflow_cycle
                        and STAGE_ROLES.get(stage.stage_type) == role for stage in previous)
                and all(row.assigned_to_id is None and row.assigned_at is None
                        and not row.notes and row.repetition_id is None
                        and row.pk not in protected_assignments for row in placeholders)
                and bool(linked)
                and any(stage.assignment_id == latest.pk and stage.is_current for stage in linked)
                and all(stage.pk not in protected_stages and stage.repetition_id is None
                        and stage.text_id == text.pk
                        and stage.workflow_cycle == text.current_workflow_cycle
                        and STAGE_ROLES.get(stage.stage_type) == role
                        and stage.iteration > 1 and stage.started_at is None
                        and stage.ended_at is None and not stage.is_completed
                        and not stage.is_skipped and not stage.imported_completed for stage in linked)
            )
            if role in ('verifier_1', 'verifier_2'):
                other_role = 'verifier_2' if role == 'verifier_1' else 'verifier_1'
                valid = valid and verifier_users.get(other_role) != canonical.assigned_to_id
            if not valid:
                report['skipped'].append((text.pk, role, latest.pk))
                continue
            if apply:
                # Free the current slot before restoring the original assignment.
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
            report['merged'] += len(placeholders)
            report['restored'] += 1
            if role in ('verifier_1', 'verifier_2'):
                verifier_users[role] = canonical.assigned_to_id
    return report


def merge(apps, schema_editor):
    report = repair_status_assignments(apps, schema_editor.connection.alias)
    print(f"\nPrzypisania: scalono {report['merged']} pustych duplikatów, "
          f"przywrócono {report['restored']} przypisań osób.")
    for text_id, role, assignment_id in report['skipped']:
        print(f"Pozostawiono do kontroli: tekst {text_id}, rola {role}, przypisanie {assignment_id}.")


class Migration(migrations.Migration):
    dependencies = [
        ('workflow', '0010_workflowstage_queued_at'),
        ('core', '0010_editrevision'),
    ]
    operations = [migrations.RunPython(merge, migrations.RunPython.noop, atomic=True)]
