from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('core', '0032_post_layout_assignments')]

    operations = [
        migrations.AlterModelOptions(name='audiocontributor', options={
            'ordering': ('name', 'pk'), 'verbose_name': 'lektor / montaż', 'verbose_name_plural': 'Lektorzy i montaż'}),
        migrations.AlterField(model_name='audiobook', name='engineer_name',
            field=models.CharField(blank=True, max_length=255, verbose_name='montaż – imię i nazwisko')),
        migrations.AlterField(model_name='audiobook', name='engineer_contact',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='engineered_books', to='core.audiocontributor', verbose_name='profil montażysty')),
    ]
