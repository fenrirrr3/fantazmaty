from django.db import migrations


def preserve_dates(apps, schema_editor):
    Audio = apps.get_model('core', 'Audiobook')
    Stage = apps.get_model('core', 'AudiobookStage')
    database = schema_editor.connection.alias
    fields = (
        ('recording', 'recording_started_at'), ('proofreading', 'proofreading_started_at'),
        ('corrections', 'corrections_started_at'), ('editing', 'editing_started_at'),
        ('awaiting_publication', 'awaiting_publication_started_at'),
    )
    for audio in Audio.objects.using(database).all().iterator(chunk_size=500):
        if Stage.objects.using(database).filter(text_id=audio.text_id).exists():
            continue
        dates = {status: getattr(audio, field) for status, field in fields}
        for status, field in sorted(fields, key=lambda item: (dates[item[0]] is None, dates[item[0]], item[0])):
            if dates[status] and status != audio.status:
                Stage.objects.using(database).create(text_id=audio.text_id, stage_type=status,
                    started_at=dates[status], is_completed=True)
        # Plain pending records have no work to import. Unknown end dates stay
        # empty; the existing status is the only evidence for the current stage.
        if audio.status != 'pending':
            stage = Stage.objects.using(database).create(text_id=audio.text_id,
                stage_type=audio.status, started_at=dates.get(audio.status),
                is_completed=audio.status == 'published')
            if audio.status != 'published':
                Audio.objects.using(database).filter(pk=audio.pk).update(active_stage_id=stage.pk)


class Migration(migrations.Migration):
    dependencies = [('core', '0027_audiobook_stages')]
    operations = [migrations.RunPython(preserve_dates, migrations.RunPython.noop)]
