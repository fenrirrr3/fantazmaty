from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workflow", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(
            model_name="workflowroleassignment",
            name="uniq_v1_v2_user_cycle",
        ),
        migrations.AddConstraint(
            model_name="workflowroleassignment",
            constraint=models.UniqueConstraint(
                models.F("text"),
                models.F("workflow_cycle"),
                models.F("assigned_to"),
                models.Case(
                    models.When(
                        role__in=("verifier_1", "verifier_2"),
                        then=models.Value(1),
                    ),
                    default=models.Value(None),
                    output_field=models.IntegerField(),
                ),
                name="uniq_v1_v2_user_cycle",
            ),
        ),
    ]
