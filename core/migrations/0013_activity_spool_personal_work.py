from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core', '0012_activity_lookup_indexes')]
    operations = [
        migrations.AddField(model_name='useractivity', name='source_key', field=models.CharField(max_length=32, unique=True, null=True, blank=True, editable=False)),
        migrations.AddField(model_name='workflowevent', name='personal_work', field=models.BooleanField(default=False, db_index=True, editable=False)),
        migrations.AddField(model_name='workflowevent', name='personal_work_description', field=models.CharField(max_length=255, blank=True, editable=False)),
    ]
