"""Dodatkowe wykonania zapisane przed polem is_additional: oznaczenie i numer wykonania liczony od prac.

Pierwsza wersja „Dodatkowego wykonania istniejącego etapu” zapisywała przydział bez znacznika (scalanie
przydziałów roli mogło go wchłonąć) i numer etapu równy numerowi przydziału (po przekazaniu pracy albo scaleniu
wychodziło np. „wyk. 3” przy jednej wcześniejszej pracy). Rozpoznanie: przydział bieżącego przebiegu, nieaktualny,
bez daty przypisania, bez powtórzenia i przekazania pracy, którego wszystkie etapy są zakończone, nieaktualne
i poza kolejką, a w tym samym przebiegu jest bieżący etap tego samego rodzaju. Tak zapisywało tylko dodatkowe
wykonanie (praca historyczna zostawia etap bieżący, powtórzenia i przekazania mają swoje zapisy).
"""
from django.db import migrations
from django.db.models import F, Max, Q


def mark(apps, schema_editor):
    database = schema_editor.connection.alias
    A = apps.get_model('workflow', 'WorkflowRoleAssignment')
    S = apps.get_model('workflow', 'WorkflowStage')
    R = apps.get_model('workflow', 'WorkflowRepetition')
    H = apps.get_model('workflow', 'WorkflowHandoff')
    snapshot_assignments, snapshot_stages = set(), set()
    for run in R.objects.using(database).only('previous_assignment_ids', 'previous_stage_ids'):
        snapshot_assignments.update(run.previous_assignment_ids or [])
        snapshot_stages.update(run.previous_stage_ids or [])
    handed = set(H.objects.using(database).values_list('previous_assignment_id', flat=True))
    handed |= set(H.objects.using(database).values_list('new_assignment_id', flat=True))
    candidates = (A.objects.using(database)
                  .filter(is_current=False, is_additional=False, assigned_at__isnull=True, repetition__isnull=True,
                          workflow_cycle=F('text__current_workflow_cycle'))
                  .exclude(pk__in=snapshot_assignments | handed))
    found = []
    for assignment in candidates:
        stages = list(S.objects.using(database).filter(assignment_id=assignment.pk))
        if not stages or any(stage.is_current or stage.is_released or not stage.is_completed or stage.repetition_id
                             or stage.pk in snapshot_stages for stage in stages):
            continue
        if not all(S.objects.using(database).filter(
                text_id=stage.text_id, workflow_cycle=stage.workflow_cycle, stage_type=stage.stage_type,
                is_current=True).exists() for stage in stages):
            continue
        found.append((assignment, stages))
    A.objects.using(database).filter(pk__in=[assignment.pk for assignment, _ in found]).update(is_additional=True)
    extra_ids = {stage.pk for _, stages in found for stage in stages}
    groups = {}
    for _, stages in found:
        for stage in stages:
            groups.setdefault((stage.text_id, stage.workflow_cycle, stage.stage_type), []).append(stage)
    for (text_id, cycle, kind), stages in groups.items():
        base = (S.objects.using(database).filter(text_id=text_id, workflow_cycle=cycle, stage_type=kind)
                .filter(Q(is_completed=True) | Q(is_current=True)).exclude(pk__in=extra_ids)
                .aggregate(number=Max('execution_number'))['number'] or 0)
        for offset, stage in enumerate(sorted(stages, key=lambda item: item.pk), start=1):
            S.objects.using(database).filter(pk=stage.pk).update(execution_number=base + offset)


class Migration(migrations.Migration):

    dependencies = [
        ('workflow', '0018_additional_execution'),
    ]

    operations = [
        migrations.RunPython(mark, migrations.RunPython.noop),
    ]
