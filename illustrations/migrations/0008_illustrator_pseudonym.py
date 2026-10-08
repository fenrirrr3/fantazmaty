from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('illustrations', '0007_publicillustrationsettings')]
    operations = [migrations.AddField(
        model_name='illustrator', name='pseudonym',
        field=models.CharField('pseudonim', max_length=100, blank=True),
    )]
