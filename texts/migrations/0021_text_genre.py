from django.db import migrations, models


def copy_existing_genres(apps, schema_editor):
    Text = apps.get_model('texts', 'Text')
    Review = apps.get_model('texts', 'Review')
    using = schema_editor.connection.alias
    rows = Review.objects.using(using).filter(copied_text_id__isnull=False).exclude(genre='')
    for text_id, genre in rows.values_list('copied_text_id', 'genre').iterator(chunk_size=500):
        Text.objects.using(using).filter(pk=text_id, genre='').update(genre=genre)


class Migration(migrations.Migration):
    dependencies = [('texts', '0020_text_tags')]
    operations = [
        migrations.AddField(model_name='text', name='genre',
                            field=models.CharField('gatunek', max_length=100, blank=True, default='')),
        migrations.RunPython(copy_existing_genres, migrations.RunPython.noop),
    ]
