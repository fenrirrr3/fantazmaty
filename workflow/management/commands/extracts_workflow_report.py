"""Raport: teksty Ekstraktów z otwartymi etapami wyłączonymi w skróconym workflow.

Komenda niczego nie zmienia. Taki etap kończy się zwykłym „Zakończ etap” i prowadzi
do najbliższego dozwolonego etapu; nieprzypisany i nierozpoczęty nie trafia do
„Teksty do wzięcia”, więc wymaga zmiany statusu w panelu admina.
"""
from django.core.management.base import BaseCommand

from workflow.catalog import EXTRACTS_STAGE_TYPES, extracts_next_stage, extracts_text_q
from workflow.models import WorkflowStage


def disabled_open_stages():
    return (
        WorkflowStage.objects.current_cycle()
        .filter(extracts_text_q('text__'), is_completed=False, ended_at__isnull=True)
        .exclude(stage_type__in=EXTRACTS_STAGE_TYPES)
        .select_related('text__anthology', 'assignment__assigned_to')
        .order_by('text__anthology__title', 'text__title', 'pk')
    )


def describe(stage):
    performer = stage.assignment.assigned_to if stage.assignment_id else None
    target = dict(WorkflowStage.StageType.choices).get(extracts_next_stage(stage.text, stage.stage_type), '–')
    if stage.started_at:
        state = f'rozpoczęty {stage.started_at:%d.%m.%Y}'
    elif performer:
        state = 'przypisany, nierozpoczęty'
    else:
        state = 'nieprzypisany – ustaw status w panelu admina'
    who = (performer.get_full_name() or performer.get_username()) if performer else 'brak wykonawcy'
    return (f'{stage.text.title} (#{stage.text_id}): {stage.get_stage_type_display()} – {state}; '
            f'{who}; po zakończeniu → {target}')


class Command(BaseCommand):
    help = 'Wypisuje teksty Ekstraktów z otwartymi etapami wyłączonymi w ich workflow (bez zmian w danych).'

    def handle(self, *args, **options):
        stages = list(disabled_open_stages())
        for stage in stages:
            self.stdout.write(describe(stage))
        if stages:
            self.stdout.write(f'Otwarte wyłączone etapy: {len(stages)}.')
        else:
            self.stdout.write('Żaden tekst Ekstraktów nie ma otwartego wyłączonego etapu.')
