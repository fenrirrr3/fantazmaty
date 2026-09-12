"""Tworzy 42 teksty demo, przeprowadzając je przez serwisy workflow."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from authors.models import Author
from texts.models import Anthology, Text, TextNote
from workflow.models import WorkflowStage
from workflow import services as flow

Stage = WorkflowStage.StageType
TARGETS = (
    Stage.READY_FOR_EDITING, Stage.EDITING, Stage.FIRST_VERIFICATION,
    Stage.AUTHOR_EDITING, Stage.SECOND_VERIFICATION, Stage.EDITING_CONTROL,
    Stage.FIRST_PROOFREADING, Stage.SECOND_PROOFREADING, Stage.THIRD_VERIFICATION,
    Stage.COORDINATOR_CONTROL, Stage.EDITOR_CONTROL, Stage.THIRD_PROOFREADING,
    Stage.FOURTH_PROOFREADING, Stage.STYLING,
)
TITLES = (
    "Miasto pod lodem", "Ostatni kartograf", "Poczta między gwiazdami",
    "Ogród zapomnianych imion", "Zegar na końcu świata", "Biblioteka burz",
    "Siedem księżyców", "Strażnik pustej bramy", "Mechaniczne serce lasu",
    "Cienie nad portem", "Atlas cudzych snów", "Wyspa bez wczoraj",
    "Szept miedzianych drzew", "Pociąg do jutra",
)
ANTHOLOGIES = ("[DEMO] Miasta przyszłości", "[DEMO] Opowieści z pogranicza", "[DEMO] Za ostatnią gwiazdą")


def advance(text, target, users, variant):
    """Zatrzymaj rzeczywisty ciąg przejść na wskazanym etapie."""
    today = timezone.localdate()
    editor = users[f"redaktor_{1 + variant % 2}"]
    verifier1, verifier2 = (users[f"weryfikator_{n}"] for n in ((1, 2) if variant % 2 == 0 else (2, 1)))
    coordinator = users["koordynator_1"]
    WorkflowStage.objects.create(text=text, stage_type=Stage.READY_FOR_EDITING)
    if target == Stage.READY_FOR_EDITING:
        return
    flow.claim_ready_for_editing(text, editor, started_at=today)
    if target == Stage.EDITING:
        return
    flow.claim_stage(text, Stage.FIRST_VERIFICATION, verifier1, started_at=today)
    flow.send_to_first_verification(text, editor, ended_at=today)
    stage = flow.start_first_verification(text, verifier1, started_at=today)
    if target == Stage.FIRST_VERIFICATION:
        return
    flow.complete_stage(stage, verifier1, today)
    flow.resume_editing(text, editor, started_at=today)
    flow.send_text_to_author(text, editor, started_at=today)
    if target == Stage.AUTHOR_EDITING:
        return
    flow.resume_editing(text, editor, started_at=today)
    flow.send_to_second_verification(text, editor, started_at=today)
    stage = flow.claim_stage(text, Stage.SECOND_VERIFICATION, verifier2, started_at=today)
    if target == Stage.SECOND_VERIFICATION:
        return
    flow.complete_stage(stage, verifier2, today)
    flow.resume_editing(text, editor, started_at=today)
    flow.finish_editing_to_coordinator(text, editor, ended_at=today)
    steps = (
        (Stage.EDITING_CONTROL, coordinator),
        (Stage.FIRST_PROOFREADING, users["korektor_1"]),
        (Stage.SECOND_PROOFREADING, users["korektor_2"]),
        (Stage.THIRD_VERIFICATION, users["weryfikator_3"]),
        (Stage.COORDINATOR_CONTROL, coordinator),
        (Stage.EDITOR_CONTROL, editor),
        (Stage.THIRD_PROOFREADING, users["korektor_3"]),
        (Stage.FOURTH_PROOFREADING, users["korektor_4"]),
        (Stage.STYLING, coordinator),
    )
    for kind, actor in steps:
        if kind == Stage.STYLING:
            # Stylowanie pozostaje do przejęcia przez istniejącego superusera.
            return
        if kind == Stage.EDITOR_CONTROL:
            # Serwis automatycznie rozpoczyna kontrolę redaktora.
            stage = flow.current_stage_queryset(text).get(stage_type=kind, is_completed=False)
        else:
            stage = flow.claim_stage(text, kind, actor, started_at=today)
        if target == kind:
            return
        flow.complete_stage(stage, actor, today)
    raise CommandError(f"Nieobsługiwany etap: {target}")


class Command(BaseCommand):
    help = "Dodaje 42 teksty demo w 3 antologiach, na 14 etapach opracowania. Wymaga seed_demo."

    @transaction.atomic
    def handle(self, *args, **options):
        slugs = [f"redaktor_{n}" for n in (1, 2)]
        slugs += [f"korektor_{n}" for n in range(1, 5)]
        slugs += [f"weryfikator_{n}" for n in range(1, 4)] + ["koordynator_1"]
        User = get_user_model()
        users = {}
        for slug in slugs:
            user = User.objects.filter(username="demo_" + slug).first()
            if user is None:
                raise CommandError("Brak ekipy demo. Najpierw uruchom: python manage.py seed_demo")
            users[slug] = user
        authors = list(Author.objects.filter(
            email__in=[f"demo.autor.{n:02d}@example.com" for n in range(1, 11)],
            is_blacklisted=False,
        ).order_by("email"))
        if not authors:
            raise CommandError("Brak autorów demo spoza czarnej listy. Najpierw uruchom seed_demo.")
        anthologies = []
        for title in ANTHOLOGIES:
            matches = list(Anthology.objects.filter(title=title)[:2])
            if len(matches) > 1:
                raise CommandError(f"Więcej niż jedna antologia: {title}. Wycofano zapis.")
            anthologies.append(matches[0] if matches else Anthology.objects.create(
                title=title, status=Anthology.Status.IN_PREPARATION,
            ))
        created = skipped = 0
        counts = {}
        for stage_index, target in enumerate(TARGETS):
            for variant in range(3):
                index = stage_index * 3 + variant + 1
                marker = f"[DEMO-WF-{index:03d}]"
                matches = list(Text.objects.filter(title__startswith=marker)[:2])
                if len(matches) > 1:
                    raise CommandError(f"Powtórzony identyfikator {marker}. Wycofano zapis.")
                if matches:
                    skipped += 1
                    continue
                text = Text(
                    title=f"{marker} {TITLES[(stage_index + variant * 5) % len(TITLES)]}",
                    anthology=anthologies[variant], length=12000 + (index * 1739) % 68000,
                    coordinator_note=f"Dane demonstracyjne. Początkowy etap: {target.label}.",
                    content_warnings="Przemoc w fikcyjnym świecie" if index % 7 == 0 else "",
                )
                text.full_clean()
                text.save()
                text.authors.add(authors[(index - 1) % len(authors)])
                if index % 6 == 0 and len(authors) > 1:
                    text.authors.add(authors[index % len(authors)])
                advance(text, target, users, variant)
                TextNote.objects.create(
                    text=text, content="Tekst demonstracyjny do sprawdzania list, przydziałów i przejść workflow.",
                    is_important=index % 5 == 0,
                )
                created += 1
                counts[target.label] = counts.get(target.label, 0) + 1
        self.stdout.write(self.style.SUCCESS(f"Dodano tekstów: {created}. Pominięto istniejących: {skipped}."))
        for label, count in counts.items():
            self.stdout.write(f"{label}: {count}")
