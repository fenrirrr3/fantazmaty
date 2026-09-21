from django.db import migrations


def fill(apps, schema_editor):
    Run=apps.get_model('workflow','WorkflowRepetition')
    Stage=apps.get_model('workflow','WorkflowStage')
    Assignment=apps.get_model('workflow','WorkflowRoleAssignment')
    alias=schema_editor.connection.alias
    for run in Run.objects.using(alias).filter(completed_at__isnull=True,canceled_at__isnull=True):
        steps=list(Stage.objects.using(alias).filter(repetition_id=run.pk).order_by('pk'))
        if not steps:continue
        first=steps[0].pk
        previous=[]
        for kind in [*run.selected_stages,'ready']:
            older=Stage.objects.using(alias).filter(text_id=run.text_id,workflow_cycle=steps[0].workflow_cycle,stage_type=kind,pk__lt=first).order_by('-execution_number','-pk')
            latest=older.first()
            if latest:
                previous.extend(older.filter(execution_number=latest.execution_number).values_list('pk',flat=True) if kind != 'ready' else [latest.pk])
        assignments=[]
        for a in Assignment.objects.using(alias).filter(repetition_id=run.pk):
            old=Assignment.objects.using(alias).filter(text_id=run.text_id,workflow_cycle=a.workflow_cycle,role=a.role,pk__lt=a.pk).order_by('-execution_number','-pk').first()
            if old:assignments.append(old.pk)
        run.previous_stage_ids=previous;run.previous_assignment_ids=assignments
        run.save(update_fields=['previous_stage_ids','previous_assignment_ids'])


class Migration(migrations.Migration):
    dependencies=[('workflow','0006_workflowrepetition_canceled_at_and_more')]
    operations=[migrations.RunPython(fill,migrations.RunPython.noop)]
