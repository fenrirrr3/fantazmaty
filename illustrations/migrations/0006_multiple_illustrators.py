from django.db import migrations, models


def copy_existing(apps, schema_editor):
    Illustration = apps.get_model('illustrations', 'Illustration')
    through = Illustration.illustrators.through
    db = schema_editor.connection.alias
    through.objects.using(db).bulk_create([
        through(illustration_id=pk, illustrator_id=artist)
        for pk, artist in Illustration.objects.using(db).exclude(illustrator_id=None).values_list('pk', 'illustrator_id')
    ])


def restore_single(apps, schema_editor):
    # A downgrade must not silently discard multiple credits.
    Illustration = apps.get_model('illustrations', 'Illustration')
    db = schema_editor.connection.alias
    from django.db.models import Count
    if Illustration.objects.using(db).annotate(n=Count('illustrators')).filter(n__gt=1).exists():
        raise RuntimeError('Nie można cofnąć migracji: tekst ma kilku ilustratorów.')
    for row in Illustration.objects.using(db).prefetch_related('illustrators'):
        artist = next(iter(row.illustrators.all()), None)
        Illustration.objects.using(db).filter(pk=row.pk).update(illustrator_id=artist.pk if artist else None)


class Migration(migrations.Migration):
    dependencies = [('illustrations', '0005_independent_illustrator_directory')]
    operations = [
        migrations.AlterField(model_name='illustrator', name='last_name', field=models.CharField('nazwisko', max_length=100, blank=True)),
        migrations.AddField(model_name='illustration', name='illustrators', field=models.ManyToManyField(blank=True, related_name='illustrations', to='illustrations.illustrator', verbose_name='ilustratorzy')),
        migrations.RunPython(copy_existing, restore_single),
        migrations.RemoveField(model_name='illustration', name='illustrator'),
    ]
