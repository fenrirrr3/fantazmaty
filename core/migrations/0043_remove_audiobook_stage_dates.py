from django.db import migrations, models


class Migration(migrations.Migration):
    """Daty etapów żyją wyłącznie w historii etapów (przeniesione w 0042)."""

    dependencies = [
        ('core', '0042_audiobook_workflow_data'),
    ]

    operations = [
        migrations.RemoveField(model_name='audiobook', name='awaiting_publication_started_at'),
        migrations.RemoveField(model_name='audiobook', name='corrections_started_at'),
        migrations.RemoveField(model_name='audiobook', name='editing_started_at'),
        migrations.RemoveField(model_name='audiobook', name='proofreading_started_at'),
        migrations.RemoveField(model_name='audiobook', name='recording_started_at'),
        migrations.AlterField(
            model_name='audiobookstage',
            name='stage_type',
            field=models.CharField(choices=[('recording', 'Nagranie'), ('proofreading', 'Korekta'), ('corrections', 'Nanoszenie poprawek'), ('editing', 'Montaż')], max_length=24, verbose_name='etap'),
        ),
    ]
