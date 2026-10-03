from django.db import migrations


def create_missing(apps, schema_editor):
    Text = apps.get_model('texts', 'Text')
    Illustration = apps.get_model('illustrations', 'Illustration')
    using = schema_editor.connection.alias
    existing = Illustration.objects.using(using).values('text_id')
    texts = Text.objects.using(using).filter(
        anthology__status='in_preparation', anthology__has_illustrations=True,
    ).exclude(pk__in=existing).order_by('pk').values_list('pk', flat=True)
    batch = []
    for pk in texts.iterator(chunk_size=500):
        batch.append(Illustration(text_id=pk, status='unassigned'))
        if len(batch) == 500:
            Illustration.objects.using(using).bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        Illustration.objects.using(using).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [('illustrations', '0001_initial')]
    operations = [migrations.RunPython(create_missing, migrations.RunPython.noop)]
