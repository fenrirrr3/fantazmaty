"""Przeniesienie produkcji audiobooków na etapy jak w procesie tekstów.

- etapy „Czeka na publikację” i „Opublikowane” znikają: publikację wyznacza data
  premiery i linki (data etapu „Opublikowane” uzupełnia brakującą premierę,
  data „Czeka na publikację” – brakujący koniec montażu);
- daty z kolumn audiobooka bez odpowiadającego etapu stają się etapami
  zakończonymi (koniec nieznany) – nic nie jest wymyślane;
- trwający etap bez daty rozpoczęcia dostaje znacznik „data nieznana”
  (wyjątek: korekta bez korektora i daty to korekta czekająca na przejęcie);
- po zakończonym nagraniu powstaje oczekująca korekta;
- kolejność etapów (position) zachowuje dotychczasowy porządek;
- status i bieżący etap są przeliczane jak w core.audiobook_services.

Funkcja jest idempotentna.
"""
from django.db import migrations

KINDS = ('recording', 'proofreading', 'corrections', 'editing')
DATE_FIELDS = {'recording': 'recording_started_at', 'proofreading': 'proofreading_started_at',
               'corrections': 'corrections_started_at', 'editing': 'editing_started_at'}
AFTER = {'recording': 'proofreading_waiting', 'proofreading': 'after_proofreading',
         'corrections': 'after_corrections', 'editing': 'awaiting_publication'}


def is_started(stage):
    return bool(stage.started_at or stage.start_unknown or stage.is_completed)


def compute(audio, stages):
    if audio.paused_at:
        return 'paused', None
    open_stages = [stage for stage in stages if not stage.is_completed]
    if open_stages:
        stage = open_stages[-1]
        return (stage.stage_type if is_started(stage) else f'{stage.stage_type}_waiting'), stage
    if audio.premiere_date or audio.youtube_url or audio.hearthis_url or audio.additional_links \
            or audio.status == 'published':
        return 'published', None
    done = [stage for stage in stages if stage.is_completed]
    return (AFTER[done[-1].stage_type] if done else 'pending'), None


def convert(apps, schema_editor):
    Audiobook = apps.get_model('core', 'Audiobook')
    Stage = apps.get_model('core', 'AudiobookStage')
    for audio in Audiobook.objects.order_by('pk'):
        legacy_status = audio.status
        active_id = audio.active_stage_id
        if active_id:
            Audiobook.objects.filter(pk=audio.pk).update(active_stage=None)
        stages = list(Stage.objects.filter(text_id=audio.text_id).order_by('pk'))
        changed_audio = set()

        # 1. Publikacja i oczekiwanie na publikację przestają być etapami.
        for stage in [s for s in stages if s.stage_type in ('published', 'awaiting_publication')]:
            if stage.stage_type == 'published':
                if not audio.premiere_date and stage.started_at:
                    audio.premiere_date = stage.started_at
                    changed_audio.add('premiere_date')
                if legacy_status != 'published' and stage.pk != active_id:
                    legacy_status = 'published'
            else:
                editing = [s for s in stages if s.stage_type == 'editing']
                last = editing[-1] if editing else None
                if last and stage.started_at and not last.ended_at and (
                        not last.started_at or last.started_at <= stage.started_at):
                    last.ended_at = stage.started_at
                    last.is_completed = True
                    last.save(update_fields=['ended_at', 'is_completed'])
            stage.delete()
        stages = [s for s in stages if s.stage_type in KINDS]

        # 2. Daty z kolumn bez etapu – etap zakończony, koniec nieznany (bieżący etap ma już wiersz).
        created = []
        for kind in KINDS:
            date = getattr(audio, DATE_FIELDS[kind], None)  # po 0043 kolumn już nie ma
            if date and not any(s.stage_type == kind and s.started_at == date for s in stages):
                if any(s.stage_type == kind and s.started_at is None for s in stages):
                    holder = next(s for s in stages if s.stage_type == kind and s.started_at is None)
                    holder.started_at = date
                    holder.save(update_fields=['started_at'])
                    continue
                stage = Stage(text_id=audio.text_id, stage_type=kind, started_at=date, is_completed=True)
                created.append(stage)

        # 3. Kolejność: dotychczasowe etapy jak dotąd, nowe według porządku produkcji.
        order = list(stages)
        for stage in created:
            index = KINDS.index(stage.stage_type)
            position = next((i for i, other in enumerate(order) if KINDS.index(other.stage_type) > index), len(order))
            order.insert(position, stage)
        for number, stage in enumerate(order, start=1):
            stage.position = number
            stage.save()

        # 4. Otwarty może być tylko bieżący etap; trwający bez daty ma „datę nieznaną”.
        for stage in order:
            if stage.is_completed:
                continue
            if stage.pk != active_id:
                stage.is_completed = True
                stage.save(update_fields=['is_completed'])
            elif not stage.started_at and not (stage.stage_type == 'proofreading' and not stage.performer_id):
                stage.start_unknown = True
                stage.save(update_fields=['start_unknown'])

        # 4b. Status zapisany w starym systemie jest faktem: zakończony etap bez wiersza
        #     dostaje wpis z nieznanymi datami (np. „Montaż (zakończony)” bez historii).
        open_stage = next((s for s in order if not s.is_completed), None)
        implied = {'awaiting_publication': 'editing'}.get(legacy_status, legacy_status)
        if open_stage is None and implied in KINDS and (not order or order[-1].stage_type != implied):
            stage = Stage.objects.create(text_id=audio.text_id, stage_type=implied, is_completed=True,
                                         position=(order[-1].position + 1) if order else 1)
            order.append(stage)

        # 5. Zakończone nagranie bez dalszego etapu – korekta czeka na przejęcie.
        open_stage = next((s for s in order if not s.is_completed), None)
        if open_stage is None and order and order[-1].stage_type == 'recording' and legacy_status != 'published' \
                and not (audio.premiere_date or audio.youtube_url or audio.hearthis_url):
            last = order[-1]
            waiting = Stage.objects.create(text_id=audio.text_id, stage_type='proofreading',
                                           position=last.position + 1, queued_at=last.ended_at)
            order.append(waiting)

        audio.status = legacy_status if legacy_status == 'published' else audio.status
        status, current = compute(audio, order)
        audio.status = status
        audio.active_stage_id = current.pk if current else None
        changed_audio |= {'status', 'active_stage'}
        audio.save(update_fields=sorted(changed_audio))


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0041_audiobook_workflow_fields'),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
