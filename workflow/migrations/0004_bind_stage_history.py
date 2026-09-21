from django.db import migrations, models

ROLES = {'editing':'editor','author_editing':'editor','editor_control':'editor',
'first_verification':'verifier_1','second_verification':'verifier_2','third_verification':'verifier_3',
'first_proofreading':'proofreader_1','second_proofreading':'proofreader_2','third_proofreading':'proofreader_3','fourth_proofreading':'proofreader_4',
'editing_control':'editing_coordinator','coordinator_control':'verification_coordinator','styling':'styling'}

def bind(apps, schema_editor):
    A=apps.get_model('workflow','WorkflowRoleAssignment'); S=apps.get_model('workflow','WorkflowStage')
    db=schema_editor.connection.alias
    for a in A.objects.using(db).all().iterator(chunk_size=500):
        S.objects.using(db).filter(text_id=a.text_id, workflow_cycle=a.workflow_cycle, stage_type__in=[k for k,v in ROLES.items() if v==a.role], assignment__isnull=True).update(assignment_id=a.pk)

    S.objects.using(db).exclude(workflow_cycle=models.F('text__current_workflow_cycle')).update(is_current=False)
    A.objects.using(db).exclude(workflow_cycle=models.F('text__current_workflow_cycle')).update(is_current=False)

class Migration(migrations.Migration):
    dependencies=[('workflow','0003_workflowrepetition_and_more')]
    operations=[migrations.RunPython(bind, migrations.RunPython.noop)]
