"""Jednorazowe wyrównanie numeru wykonania etapu do numeru przydziału.

Przed poprawką w ``_assign_role`` nowy przydział roli dostawał kolejny numer wykonania,
a otwarty etap zostawał z poprzednim – stąd „Pierwsza korekta” obok „Korektor 1 (wyk. 2)”.
Komenda poprawia istniejące dane: etapy bieżące i niezakończone, których numer różni się
od numeru przydziału. Etapy po przekazaniu („Przekaż etap”) są pomijane – tam różnica
jest zamierzona, bo przekazanie kontynuuje to samo wykonanie.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from workflow.models import WorkflowStage


def misaligned_stages():
    return (
        WorkflowStage.objects
        .filter(is_current=True, is_completed=False, assignment__isnull=False)
        .exclude(execution_number=F("assignment__execution_number"))
        .exclude(handoffs__isnull=False)
        .select_related("text", "assignment")
        .order_by("text__title", "pk")
    )


class Command(BaseCommand):
    help = "Wyrównuje numer wykonania otwartych etapów do numeru ich przydziału (jednorazowa naprawa danych)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Tylko wypisz, co zostałoby zmienione.")

    def handle(self, *args, dry_run=False, **options):
        stages = list(misaligned_stages())
        for stage in stages:
            self.stdout.write(
                f"{stage.text.title}: {stage.get_stage_type_display()} "
                f"wyk. {stage.execution_number} -> {stage.assignment.execution_number}"
            )
        if not stages:
            self.stdout.write("Wszystkie otwarte etapy mają numer zgodny z przydziałem.")
            return
        if dry_run:
            self.stdout.write(f"Do zmiany: {len(stages)} (bez zapisu).")
            return
        with transaction.atomic():
            for stage in stages:
                WorkflowStage.objects.filter(pk=stage.pk).update(execution_number=stage.assignment.execution_number)
        self.stdout.write(f"Poprawiono: {len(stages)}.")
