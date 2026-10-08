from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0020_authentication_attempt_limits')]
    operations = [migrations.AddField(
        model_name='mailboxconnection', name='purpose',
        field=models.CharField('przeznaczenie', max_length=20,
            choices=[('submissions', 'Zgłoszenia tekstów'), ('recruitment', 'Rekrutacja do zespołu')],
            default='submissions'),
    )]
