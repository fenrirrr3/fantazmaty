from django.db import migrations


def novels_not_for_recording(apps, schema_editor):
    Text = apps.get_model('texts', 'Text')
    Text.objects.using(schema_editor.connection.alias).filter(
        anthology__is_novel=True, for_recording=True).update(for_recording=False)


class Migration(migrations.Migration):
    """Powieści nie są nagrywane – rozdziały znikają z kolejki „Audiobooki do nagrywania” w panelu admina."""

    dependencies = [
        ('texts', '0039_remove_import_leftovers'),
    ]

    operations = [
        migrations.RunPython(novels_not_for_recording, migrations.RunPython.noop),
    ]
