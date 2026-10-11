"""Audiobooki: jeden stały ciąg etapów i sześć statusów.

Do wzięcia → Nagrywanie → Korekta → Nanoszenie poprawek → Montaż → Opublikowane.

- znikają statusy pośrednie („Czeka na…”, „Po korekcie”, „Po poprawkach”,
  „Oczekuje na publikację”) i wstrzymanie prac (status wynika z etapów);
- wiersze etapów spoza czterech rodzajów (stare dane) są usuwane;
- nierozpoczęte nagrywanie znika – tekst wraca do „Do wzięcia”;
- po zakończonym etapie bez następnego powstaje następny, czekający na rozpoczęcie
  (data dostępności = koniec poprzedniego); po montażu – Opublikowane.

Funkcja jest idempotentna. Daty nie są wymyślane.
"""
import django.db.models.deletion
from django.db import migrations, models

KINDS = ('recording', 'proofreading', 'corrections', 'editing')
NEXT = dict(zip(KINDS, KINDS[1:]))


def is_started(stage):
    return bool(stage.started_at or stage.start_unknown or stage.is_completed)


def convert(apps, schema_editor):
    Audiobook = apps.get_model('core', 'Audiobook')
    Stage = apps.get_model('core', 'AudiobookStage')
    for audio in Audiobook.objects.order_by('pk'):
        Audiobook.objects.filter(pk=audio.pk).update(active_stage=None)
        stages = []
        for stage in Stage.objects.filter(text_id=audio.text_id).order_by('position', 'pk'):
            if stage.stage_type not in KINDS or (stage.stage_type == 'recording' and not is_started(stage)):
                stage.delete()
            else:
                stages.append(stage)
        open_stages = [s for s in stages if not s.is_completed]
        for stage in open_stages[:-1]:  # otwarty może być tylko bieżący etap
            stage.is_completed = True
            stage.save(update_fields=['is_completed'])
        published = bool(audio.premiere_date or audio.youtube_url or audio.hearthis_url or audio.additional_links
                         or audio.status in ('published', 'awaiting_publication'))
        current = open_stages[-1] if open_stages else None
        if current is None and stages and stages[-1].stage_type in NEXT and not published:
            last = stages[-1]
            current = Stage.objects.create(text_id=audio.text_id, stage_type=NEXT[last.stage_type],
                                           position=last.position + 1, queued_at=last.ended_at)
        if current is not None:
            status = current.stage_type
        elif published or (stages and stages[-1].stage_type == 'editing'):
            status = 'published'
        else:
            status = 'pending'
        values = {'status': status, 'active_stage': current}
        if any(field.name == 'paused_at' for field in Audiobook._meta.get_fields()):
            values['paused_at'] = None
        Audiobook.objects.filter(pk=audio.pk).update(**values)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0043_remove_audiobook_stage_dates"),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="audiobook",
            name="paused_at",
        ),
        migrations.AlterField(
            model_name="audiobook",
            name="active_stage",
            field=models.OneToOneField(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="active_for",
                to="core.audiobookstage",
                verbose_name="bieżący etap",
            ),
        ),
        migrations.AlterField(
            model_name="audiobook",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Do wzięcia"),
                    ("recording", "Nagrywanie"),
                    ("proofreading", "Korekta"),
                    ("corrections", "Nanoszenie poprawek"),
                    ("editing", "Montaż"),
                    ("published", "Opublikowane"),
                ],
                db_index=True,
                default="pending",
                max_length=24,
                verbose_name="status audiobooka",
            ),
        ),
        migrations.AlterField(
            model_name="audiobookstage",
            name="stage_type",
            field=models.CharField(
                choices=[
                    ("recording", "Nagrywanie"),
                    ("proofreading", "Korekta"),
                    ("corrections", "Nanoszenie poprawek"),
                    ("editing", "Montaż"),
                ],
                max_length=24,
                verbose_name="etap",
            ),
        ),
    ]
