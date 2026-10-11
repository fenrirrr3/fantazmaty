import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Pola etapów jak w procesie tekstów. Stare kolumny dat usuwa 0043 po przeniesieniu danych."""

    dependencies = [
        ('core', '0040_correction_reporter_name'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='audiobookstage',
            options={'ordering': ('position', 'pk'), 'verbose_name': 'etap audiobooka', 'verbose_name_plural': 'Historia etapów audiobooków'},
        ),
        migrations.AddField(
            model_name='audiobook',
            name='paused_at',
            field=models.DateField(blank=True, editable=False, null=True, verbose_name='prace wstrzymane od'),
        ),
        migrations.AddField(
            model_name='audiobookstage',
            name='position',
            field=models.PositiveIntegerField(db_index=True, default=0, verbose_name='kolejność'),
        ),
        migrations.AddField(
            model_name='audiobookstage',
            name='queued_at',
            field=models.DateField(blank=True, null=True, verbose_name='oczekuje od'),
        ),
        migrations.AddField(
            model_name='audiobookstage',
            name='start_unknown',
            field=models.BooleanField(default=False, help_text='Etap trwał lub trwa, ale data rozpoczęcia nie jest znana.', verbose_name='rozpoczęty (data nieznana)'),
        ),
        migrations.AlterField(
            model_name='audiobook',
            name='active_stage',
            field=models.OneToOneField(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='active_for', to='core.audiobookstage', verbose_name='bieżący etap (oczekujący lub trwający)'),
        ),
        migrations.AlterField(
            model_name='audiobook',
            name='status',
            field=models.CharField(choices=[('pending', 'Otwarte'), ('recording_waiting', 'Czeka na nagranie'), ('recording', 'Trwa nagranie'), ('proofreading_waiting', 'Czeka na korektę'), ('proofreading', 'Trwa korekta'), ('after_proofreading', 'Po korekcie'), ('corrections_waiting', 'Czeka na poprawki'), ('corrections', 'Nanoszenie poprawek'), ('after_corrections', 'Po poprawkach'), ('editing_waiting', 'Oczekuje na montaż'), ('editing', 'Trwa montaż'), ('awaiting_publication', 'Oczekuje na publikację'), ('published', 'Opublikowane'), ('paused', 'Prace wstrzymane')], db_index=True, default='pending', max_length=24, verbose_name='status audiobooka'),
        ),
    ]
