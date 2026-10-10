from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('workflow', '0015_historical_verifiers_5_7')]
    operations = [migrations.AlterField(model_name='workflowhandoff', name='reason', field=models.TextField(blank=True))]
