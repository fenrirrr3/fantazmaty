from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0011_remove_discorddispatch')]
    operations = [
        migrations.AddIndex(model_name='useractivity', index=models.Index(fields=['user', 'created_at', 'id'], name='activity_user_time_idx')),
        migrations.AddIndex(model_name='workflowevent', index=models.Index(fields=['actor', 'created_at', 'id'], name='workflow_actor_time_idx')),
    ]
