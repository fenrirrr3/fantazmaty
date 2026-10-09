"""One-off, guarded repair of two records from the Ballady September import."""
from datetime import date
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A

SOURCE = 'ballady-2026-09-29-v1'
BOOK = 'Ballady ze spalonego traktu'
TARGETS = [(1, 'Preludium', date(2026, 9, 16)), (4, 'Kolorowych koszmarów', date(2026, 9, 24))]


@transaction.atomic
def repair(*, apply=False):
    plans = []
    for row, title, transition in TARGETS:
        candidates = list(Text.objects.select_for_update().filter(import_source=SOURCE, import_source_row=row)[:2])
        if len(candidates) != 1:
            raise CommandError(f'Nie znaleziono jednoznacznie tekstu „{title}” z importu {SOURCE}. Niczego nie zmieniono.')
        text = candidates[0]
        if text.title != title or not text.anthology_id or text.anthology.title != BOOK or text.anthology.status != 'in_preparation' or text.current_workflow_cycle != 1:
            raise CommandError(f'„{title}”: zmieniono tekst, antologię lub przebieg. Wymaga ręcznego sprawdzenia; niczego nie zmieniono.')
        stages = list(S.objects.select_for_update().filter(text=text).select_related('assignment').order_by('pk'))
        later = [s for s in stages if s.stage_type in ('editing_control', 'first_proofreading') and s.imported_completed and s.is_completed and s.iteration == 1]
        author = [s for s in stages if s.stage_type == 'author_editing' and s.iteration == 1]
        if len(later) != 2 or len(author) != 1:
            raise CommandError(f'„{title}”: inna historia niż w pierwotnym imporcie; niczego nie zmieniono.')
        author = author[0]
        if all(not s.is_current and not s.is_released for s in later) and author.started_at == transition:
            plans.append((text, [], None))
            continue
        expected_count = 7 if row == 1 else 5
        verification = [
            s
            for s in stages
            if s.stage_type == "first_verification"
            and s.imported_completed
            and s.is_completed
            and s.ended_at == transition
        ]
        current_editor = list(A.objects.filter(text=text, role="editor", is_current=True))
        if (
            len(stages) != expected_count
            or len(verification) != 1
            or len(current_editor) != 1
            or author.assignment_id != current_editor[0].pk
            or author.started_at is not None
            or author.ended_at is not None
            or author.is_completed
            or not author.is_current
            or not author.is_released
            or any(not s.is_current or not s.is_released for s in later)
            or any(s.repetition_id for s in stages)
            or any(s != author and (not s.is_completed or not s.imported_completed) for s in stages)
        ):
            raise CommandError(
                f"„{title}”: dane zmieniły się od importu. Nie nadpisano zmian; cała naprawa wycofana."
            )
        plans.append((text, later, author))
    report = []
    for text, later, author in plans:
        if author is None:
            report.append(f"{text.title}: już naprawiono, bez zmian.")
            continue
        transition = next(d for _, title, d in TARGETS if title == text.title)
        if apply:
            for stage in later:
                stage.is_current = False
                stage.is_released = False
                stage.full_clean()
                stage.save(update_fields=["is_current", "is_released"])
            author.started_at = transition
            author.full_clean()
            author.save(update_fields=["started_at"])
        report.append(
            f"{text.title}: kontrola redakcji i pierwsza korekta pozostają zakończone w historii; przekazanie autorowi {transition.isoformat()}."
        )
    return report


class Command(BaseCommand):
    help = "Naprawia dwa rekordy Ballad bez usuwania tekstów i przypisań. Domyślnie tylko podgląd."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Zapisz naprawę w jednej transakcji."
        )

    def handle(self, *args, **options):
        report = repair(apply=options["apply"])
        for line in report:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                "Zapis zakończony."
                if options["apply"]
                else "Podgląd: niczego nie zmieniono. Zapis: --apply."
            )
        )
