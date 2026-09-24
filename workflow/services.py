from workflow.catalog import active_stage_choices
from workflow.anthology_policy import require_working_anthology
from datetime import date, datetime, timedelta
from functools import wraps
from core.workflow_events import track_workflow

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.db.models import Max
from django.utils import timezone

from core.permissions import (
    get_active_person_profile,
    has_role,
    is_coordinator,
)
from texts.models import Text

from .models import WorkflowRoleAssignment, WorkflowStage


StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role


ROLE_GROUPS = {
    Role.EDITOR: "Redaktor",
    Role.EDITING_COORDINATOR: "Koordynator",
    Role.PROOFREADER_1: "Korektor",
    Role.PROOFREADER_2: "Korektor",
    Role.PROOFREADER_3: "Korektor",
    Role.PROOFREADER_4: "Korektor",
    Role.VERIFIER_1: "Weryfikator",
    Role.VERIFIER_2: "Weryfikator",
    Role.VERIFIER_3: "Weryfikator",
    Role.VERIFICATION_COORDINATOR: "Koordynator",
    Role.STYLING: "Koordynator",
}

STAGE_ROLES = {
    StageType.EDITING: Role.EDITOR,
    StageType.FIRST_VERIFICATION: Role.VERIFIER_1,
    StageType.AUTHOR_EDITING: Role.EDITOR,
    StageType.SECOND_VERIFICATION: Role.VERIFIER_2,
    StageType.EDITING_CONTROL: Role.EDITING_COORDINATOR,
    StageType.FIRST_PROOFREADING: Role.PROOFREADER_1,
    StageType.SECOND_PROOFREADING: Role.PROOFREADER_2,
    StageType.THIRD_VERIFICATION: Role.VERIFIER_3,
    StageType.COORDINATOR_CONTROL: Role.VERIFICATION_COORDINATOR,
    StageType.EDITOR_CONTROL: Role.EDITOR,
    StageType.THIRD_PROOFREADING: Role.PROOFREADER_3,
    StageType.FOURTH_PROOFREADING: Role.PROOFREADER_4,
    StageType.STYLING: Role.STYLING,
}

RESTARTABLE_STAGE_TYPES = (
    StageType.READY_FOR_EDITING,
    StageType.EDITING,
    StageType.FIRST_VERIFICATION,
    StageType.AUTHOR_EDITING,
    StageType.SECOND_VERIFICATION,
    StageType.EDITING_CONTROL,
    StageType.FIRST_PROOFREADING,
    StageType.SECOND_PROOFREADING,
    StageType.THIRD_VERIFICATION,
    StageType.COORDINATOR_CONTROL,
    StageType.EDITOR_CONTROL,
    StageType.THIRD_PROOFREADING,
    StageType.FOURTH_PROOFREADING,
    StageType.STYLING,
)

NEXT_STAGE_TYPES = {
    StageType.FIRST_VERIFICATION: StageType.EDITING,
    StageType.SECOND_VERIFICATION: StageType.EDITING,
    StageType.EDITING_CONTROL: StageType.FIRST_PROOFREADING,
    StageType.FIRST_PROOFREADING: StageType.SECOND_PROOFREADING,
    StageType.SECOND_PROOFREADING: StageType.THIRD_VERIFICATION,
    StageType.THIRD_VERIFICATION: StageType.COORDINATOR_CONTROL,
    StageType.COORDINATOR_CONTROL: StageType.EDITOR_CONTROL,
    StageType.EDITOR_CONTROL: StageType.THIRD_PROOFREADING,
    StageType.THIRD_PROOFREADING: StageType.FOURTH_PROOFREADING,
    StageType.FOURTH_PROOFREADING: StageType.STYLING,
    StageType.STYLING: StageType.READY,
}

EDITING_PHASE_TYPES = {
    StageType.READY_FOR_EDITING,
    StageType.EDITING,
    StageType.FIRST_VERIFICATION,
    StageType.AUTHOR_EDITING,
    StageType.SECOND_VERIFICATION,
}


def belongs_to_group(user, group_name):
    """Zgodność z dotychczasowymi wywołaniami; wspólna polityka ról."""
    return has_role(user, group_name)


def _database(instance):
    return instance._state.db or router.db_for_write(
        type(instance),
        instance=instance,
    )


def _actor_is_active(user):
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and user.pk is not None
        and (
            user.is_superuser
            or get_active_person_profile(user) is not None
        )
    )


def _ensure_actor(user):
    if not _actor_is_active(user):
        raise PermissionDenied(
            "Operacja wymaga aktywnego konta i przynależności do zespołu."
        )


def _validate_date(value):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValidationError("Podaj poprawną datę bez godziny.")

    return value


def _transition_date(value):
    return _validate_date(
        timezone.localdate() if value is None else value
    )


def _locked_text_operation(function):
    """Każda mutacja blokuje najpierw nadrzędny rekord Text."""

    @wraps(function)
    def wrapped(text, *args, **kwargs):
        if text.pk is None:
            raise ValidationError("Tekst musi być zapisany.")

        using = _database(text)

        with transaction.atomic(using=using):
            locked_text = (
                Text.objects.using(using)
                .select_for_update()
                .get(pk=text.pk)
            )

            if (
                text.current_workflow_cycle
                != locked_text.current_workflow_cycle
            ):
                raise ValidationError(
                    "Przebieg workflow zmienił się. "
                    "Odśwież stronę przed wykonaniem operacji."
                )

            require_working_anthology(locked_text)
            result = function(locked_text, *args, **kwargs)

        text.current_workflow_cycle = locked_text.current_workflow_cycle
        return result

    return track_workflow(wrapped)


def _locked_stage_operation(function):
    """Zachowuje kolejność blokowania: Text, następnie WorkflowStage."""

    @wraps(function)
    def wrapped(stage, *args, **kwargs):
        if stage.pk is None:
            raise ValidationError("Etap musi być zapisany.")

        using = _database(stage)

        with transaction.atomic(using=using):
            text_id = (
                WorkflowStage.objects.using(using)
                .values_list("text_id", flat=True)
                .get(pk=stage.pk)
            )
            locked_text = (
                Text.objects.using(using)
                .select_for_update()
                .get(pk=text_id)
            )
            locked_stage = (
                WorkflowStage.objects.using(using)
                .select_for_update()
                .get(pk=stage.pk)
            )

            if locked_stage.text_id != locked_text.pk:
                raise ValidationError(
                    "Powiązanie etapu zmieniło się. Odśwież stronę."
                )

            require_working_anthology(locked_text)
            locked_stage.text = locked_text
            ensure_stage_belongs_to_current_cycle(locked_stage)
            return function(locked_stage, *args, **kwargs)

    return track_workflow(wrapped)


def current_cycle(text):
    return text.current_workflow_cycle


def current_stage_queryset(text):
    return WorkflowStage.objects.using(_database(text)).current_cycle().filter(
        text_id=text.pk,
        workflow_cycle=current_cycle(text),
        is_current=True,
    )


def current_assignment_queryset(text):
    return WorkflowRoleAssignment.objects.using(_database(text)).current_cycle().filter(
        text_id=text.pk,
        workflow_cycle=current_cycle(text),
        is_current=True,
    )


def stage_belongs_to_current_cycle(stage):
    if stage.text_id is None:
        return False

    return stage.is_current and stage.is_released and stage.workflow_cycle == stage.text.current_workflow_cycle


def ensure_stage_belongs_to_current_cycle(stage):
    if not stage_belongs_to_current_cycle(stage):
        raise ValidationError(
            "Nie można zmieniać etapu należącego do "
            "poprzedniego przebiegu workflow."
        )


def validate_assignment_start_date(started_at):
    started_at = _validate_date(started_at)
    today = timezone.localdate()

    if started_at < today:
        raise ValidationError(
            "Data rozpoczęcia nie może być wcześniejsza "
            "niż dzisiejsza data."
        )

    if started_at > today + timedelta(days=14):
        raise ValidationError(
            "Data rozpoczęcia nie może przypadać później "
            "niż 14 dni od dzisiejszej daty."
        )

    return started_at


def ensure_distinct_primary_verifier(text, role, user):
    conflicting_role = {
        Role.VERIFIER_1: Role.VERIFIER_2,
        Role.VERIFIER_2: Role.VERIFIER_1,
    }.get(role)

    if conflicting_role is None:
        return

    if current_assignment_queryset(text).filter(
        role=conflicting_role,
        assigned_to_id=user.pk,
    ).exists():
        raise ValidationError(
            "Pierwszą i drugą weryfikację tego tekstu muszą "
            "wykonywać różne osoby."
        )


def cycle_entry_stage_is(text, stage_type):
    stages = list(
        current_stage_queryset(text)
        .order_by("pk")
        .values_list("stage_type", "is_completed")[:2]
    )
    return stages == [(stage_type, False)]


def text_is_withdrawn(text):
    return current_stage_queryset(text).filter(
        stage_type=StageType.WITHDRAWN,
    ).exists()


def ensure_text_is_not_withdrawn(text):
    if text_is_withdrawn(text):
        raise ValidationError(
            "Tekst został wycofany. Dalsze prace i zmiany "
            "statusów są zablokowane."
        )


def _ensure_editing_phase(text):
    if text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).exists():
        raise ValidationError("W powtórzeniu użyj przycisku Zakończ etap; kolejność wynika z wybranej kolejki.")
    if current_stage_queryset(text).exclude(
        stage_type__in=EDITING_PHASE_TYPES,
    ).exists():
        raise ValidationError(
            "Tekst przeszedł już do dalszej części procesu. "
            "Powrót wymaga rozpoczęcia nowego przebiegu przez superusera."
        )


def completed_stage_exists(text, stage_type):
    return current_stage_queryset(text).filter(
        stage_type=stage_type,
        is_completed=True,
    ).exists()


def active_stage_exists(text, stage_type):
    return current_stage_queryset(text).filter(
        stage_type=stage_type,
        is_completed=False,
        started_at__isnull=False,
        ended_at__isnull=True,
    ).exists()


def get_active_stage(text, stage_type):
    return (
        current_stage_queryset(text)
        .filter(
            stage_type=stage_type,
            is_completed=False,
            started_at__isnull=False,
            ended_at__isnull=True,
        )
        .order_by("-iteration", "-pk")
        .first()
    )


def next_iteration(text, stage_type):
    maximum = (
        WorkflowStage.objects.filter(text=text, workflow_cycle=current_cycle(text), stage_type=stage_type)
        .aggregate(maximum=Max("iteration"))["maximum"]
        or 0
    )
    return maximum + 1


def _create_pending_stage(text, stage_type):
    """Wewnętrzna operacja; wywołujący musi posiadać blokadę Text."""
    ensure_text_is_not_withdrawn(text)

    if stage_type not in dict(active_stage_choices()):
        raise ValidationError("Nieprawidłowy typ etapu.")

    stage = (
        current_stage_queryset(text)
        .filter(stage_type=stage_type, is_completed=False, is_released=True)
        .order_by("-iteration", "-pk")
        .first()
    )

    if stage is not None:
        return stage

    return WorkflowStage.objects.using(_database(text)).create(
        text=text,
        workflow_cycle=current_cycle(text),
        stage_type=stage_type,
        iteration=next_iteration(text, stage_type),
        started_at=None,
        ended_at=None,
        is_completed=False,
    )


@_locked_text_operation
def create_pending_stage(text, stage_type):
    """Techniczny helper dla zaufanego kodu; nie jest akcją użytkownika."""
    return _create_pending_stage(text, stage_type)


def user_is_assigned_editor(text, user):
    if not _actor_is_active(user):
        return False

    return current_assignment_queryset(text).filter(
        role=Role.EDITOR,
        assigned_to_id=user.pk,
    ).exists()


def ensure_editor_access(text, user):
    _ensure_actor(user)

    if not (
        user_is_assigned_editor(text, user)
        or is_coordinator(user)
    ):
        raise PermissionDenied(
            "Tę operację może wykonać przypisany redaktor "
            "lub koordynator."
        )


def _start_stage(stage, started_at):
    started_at = _validate_date(started_at)
    ensure_stage_belongs_to_current_cycle(stage)

    if stage.is_completed or stage.started_at or stage.ended_at:
        raise ValidationError("Ten etap nie oczekuje na rozpoczęcie.")

    latest_end = (
        current_stage_queryset(stage.text)
        .filter(is_completed=True)
        .aggregate(latest=Max("ended_at"))["latest"]
    )

    if latest_end is not None and started_at < latest_end:
        raise ValidationError(
            "Data rozpoczęcia nie może poprzedzać zakończenia "
            "wcześniejszych prac w tym przebiegu."
        )

    stage.started_at = started_at
    stage.save(update_fields=["started_at"])
    return stage


def _finish_stage_record(stage, ended_at):
    ended_at = _validate_date(ended_at)
    ensure_stage_belongs_to_current_cycle(stage)
    ensure_text_is_not_withdrawn(stage.text)

    if stage.is_completed:
        raise ValidationError("Ten etap został już zakończony.")

    if not stage.started_at:
        raise ValidationError(
            "Nie można zakończyć nierozpoczętego etapu."
        )

    if ended_at > timezone.localdate():
        raise ValidationError("Nie można zakończyć pracy z przyszłą datą.")
    if ended_at < stage.started_at:
        raise ValidationError(
            "Data zakończenia nie może być wcześniejsza "
            "niż data rozpoczęcia."
        )

    stage.ended_at = ended_at
    stage.is_completed = True
    stage.save(update_fields=["ended_at", "is_completed"])
    return stage


@_locked_stage_operation
def finish_stage_record(stage, ended_at):
    """Techniczny helper; uprawnienia sprawdza operacja wywołująca."""
    return _finish_stage_record(stage, ended_at)


def _assign_role(text, role, user):
    from people.leave_access import require_available
    require_available(user)
    if role == Role.STYLING and not user.is_superuser:
        raise PermissionDenied("Stylowanie może przejąć tylko superuser.")
    ensure_distinct_primary_verifier(text, role, user)

    assignment = current_assignment_queryset(text).filter(
        role=role,
    ).first()

    if assignment is None:
        return WorkflowRoleAssignment.objects.using(
            _database(text)
        ).create(
            text=text,
            workflow_cycle=current_cycle(text),
            role=role,
            assigned_to=user,
        )

    if assignment.assigned_to_id not in (None, user.pk):
        raise ValidationError("Ta rola jest już przypisana innej osobie.")

    if assignment.assigned_to_id is None:
        assignment.assigned_to = user
        assignment.save(update_fields=["assigned_to"])

    return assignment






@_locked_text_operation
def claim_ready_for_editing(text, user, started_at=None):
    _ensure_actor(user)
    ensure_text_is_not_withdrawn(text)

    if not (
        belongs_to_group(user, "Redaktor")
        or is_coordinator(user)
    ):
        raise PermissionDenied(
            "Etap może przejąć redaktor lub koordynator."
        )

    transition_date = validate_assignment_start_date(
        _transition_date(started_at)
    )
    ready_stage = current_stage_queryset(text).filter(
        stage_type=StageType.READY_FOR_EDITING,
        is_completed=False,
    ).first()

    if ready_stage is None:
        raise ValidationError(
            "Tekst nie znajduje się na etapie „Do redakcji”."
        )

    if current_assignment_queryset(text).filter(
        role=Role.EDITOR,
        assigned_to__isnull=False,
    ).exists():
        raise ValidationError("Do tego tekstu przypisano już redaktora.")

    _assign_role(text, Role.EDITOR, user)
    ready_stage.delete()

    editing_stage = _create_pending_stage(text, StageType.EDITING)
    _start_stage(editing_stage, transition_date)
    _create_pending_stage(text, StageType.FIRST_VERIFICATION)
    return editing_stage


@_locked_text_operation
def claim_stage(text, stage_type, user, started_at=None):
    _ensure_actor(user)
    ensure_text_is_not_withdrawn(text)

    role = STAGE_ROLES.get(stage_type)
    if role == Role.STYLING and not user.is_superuser:
        raise PermissionDenied("Stylowanie może przejąć tylko superuser.")
    required_group = ROLE_GROUPS.get(role)

    if role is None or required_group is None:
        raise ValidationError("Tego etapu nie można przypisać ręcznie.")

    if not (
        belongs_to_group(user, required_group)
        or is_coordinator(user)
    ):
        raise PermissionDenied(f"Wymagana rola: {required_group}.")

    transition_date = validate_assignment_start_date(
        _transition_date(started_at)
    )
    stage = (
        current_stage_queryset(text)
        .filter(stage_type=stage_type, is_completed=False, is_released=True)
        .order_by("-iteration", "-pk")
        .first()
    )

    if stage is None:
        raise ValidationError("Ten etap nie jest jeszcze dostępny.")
    if stage.repetition_id:
        from workflow.repetitions import claim_repeat
        return claim_repeat(text, stage, user, transition_date)
    from workflow.availability import claim_reason
    reason = claim_reason(stage,user,list(current_stage_queryset(text)),list(current_assignment_queryset(text)))
    if reason: raise ValidationError(reason)
    is_cycle_entry = cycle_entry_stage_is(text, stage_type)
    if stage_type == StageType.FIRST_VERIFICATION and started_at is not None:
        raise ValidationError('Rezerwacja pierwszej weryfikacji nie ustala daty. Datę podaje się przy rozpoczęciu pracy.')

    _assign_role(text, role, user)

    # Pierwszy weryfikator rezerwuje pracę. Rozpoczęcie następuje
    # oddzielnie, po przekazaniu tekstu przez redaktora.
    if stage_type == StageType.FIRST_VERIFICATION:
        return stage

    _start_stage(stage, transition_date)

    if stage_type == StageType.EDITING and is_cycle_entry:
        _create_pending_stage(text, StageType.FIRST_VERIFICATION)

    return stage


@_locked_text_operation
def send_to_first_verification(text, user, ended_at=None):
    ensure_editor_access(text, user)
    ensure_text_is_not_withdrawn(text)
    _ensure_editing_phase(text)

    transition_date = _transition_date(ended_at)

    verification_stage = current_stage_queryset(text).filter(
        stage_type=StageType.FIRST_VERIFICATION,
        is_completed=False,
        started_at__isnull=True,
        ended_at__isnull=True,
    ).first()
    editing_stage = get_active_stage(text, StageType.EDITING)

    if editing_stage is None:
        raise ValidationError("Nie ma aktywnej redakcji do przekazania.")
    if completed_stage_exists(text, StageType.FIRST_VERIFICATION):
        raise ValidationError("Pierwsza weryfikacja jest już zakończona.")
    if current_stage_queryset(text).filter(
        stage_type=StageType.FIRST_VERIFICATION, is_completed=False,
        started_at__isnull=False,
    ).exists():
        raise ValidationError("Pierwsza weryfikacja została już rozpoczęta.")
    if verification_stage is None:
        verification_stage = _create_pending_stage(text, StageType.FIRST_VERIFICATION)

    _finish_stage_record(editing_stage, transition_date)
    return verification_stage


@_locked_text_operation
def start_first_verification(text, user, started_at=None):
    from people.leave_access import require_available
    require_available(user)
    _ensure_actor(user)
    ensure_text_is_not_withdrawn(text)
    _ensure_editing_phase(text)

    transition_date = validate_assignment_start_date(
        _transition_date(started_at)
    )
    assignment = current_assignment_queryset(text).filter(
        role=Role.VERIFIER_1,
        assigned_to__isnull=False,
    ).first()

    if assignment is None:
        raise ValidationError(
            "Do pierwszej weryfikacji nie przypisano osoby."
        )

    require_available(assignment.assigned_to)

    if assignment.assigned_to_id != user.pk and not is_coordinator(user):
        raise PermissionDenied(
            "Pierwszą weryfikację może rozpocząć przypisany "
            "weryfikator lub koordynator."
        )

    if any(
        active_stage_exists(text, kind)
        for kind in (StageType.EDITING, StageType.AUTHOR_EDITING)
    ):
        raise ValidationError(
            "Tekst nadal znajduje się u redaktora lub autora."
        )

    stage = current_stage_queryset(text).filter(
        stage_type=StageType.FIRST_VERIFICATION,
        is_completed=False,
        started_at__isnull=True,
        ended_at__isnull=True,
    ).first()

    if stage is None:
        raise ValidationError(
            "Pierwsza weryfikacja nie oczekuje na rozpoczęcie."
        )

    return _start_stage(stage, transition_date)


@_locked_text_operation
def resume_editing(text, user, started_at=None):
    from people.leave_access import require_available
    require_available(user)
    ensure_editor_access(text, user)
    assigned_editor = current_assignment_queryset(text).filter(role=Role.EDITOR, assigned_to__isnull=False).first()
    if assigned_editor:
        require_available(assigned_editor.assigned_to)
    ensure_text_is_not_withdrawn(text)
    _ensure_editing_phase(text)

    transition_date = _transition_date(started_at)

    if active_stage_exists(text, StageType.EDITING):
        raise ValidationError("Redakcja jest już aktywna.")

    # Blokuje również przejętą lub oczekującą weryfikację po przekazaniu
    # tekstu. Nie można wznowić redakcji, omijając jej wykonanie.
    if current_stage_queryset(text).filter(
        stage_type__in=(
            StageType.FIRST_VERIFICATION,
            StageType.SECOND_VERIFICATION,
        ),
        is_completed=False,
    ).exists():
        raise ValidationError(
            "Najpierw należy zakończyć oczekującą lub aktywną weryfikację."
        )

    author_stage = current_stage_queryset(text).filter(
        stage_type=StageType.AUTHOR_EDITING, is_completed=False,
    ).first()
    if author_stage is not None and author_stage.started_at is None:
        raise ValidationError(
            "Etap pracy autora nie ma daty rozpoczęcia. Uzupełnij ją w panelu administratora przed wznowieniem redakcji."
        )

    if not (
        author_stage
        or completed_stage_exists(text, StageType.FIRST_VERIFICATION)
        or completed_stage_exists(text, StageType.SECOND_VERIFICATION)
    ):
        raise ValidationError("Redakcja nie może jeszcze zostać wznowiona.")

    if author_stage is not None:
        _finish_stage_record(author_stage, transition_date)

    editing_stage = _create_pending_stage(text, StageType.EDITING)
    return _start_stage(editing_stage, transition_date)


def editing_checkpoint_passed(text, checkpoint):
    return completed_stage_exists(text, checkpoint)


@_locked_text_operation
def send_text_to_author(text, user, started_at=None):
    ensure_editor_access(text, user)
    ensure_text_is_not_withdrawn(text)
    _ensure_editing_phase(text)

    transition_date = _transition_date(started_at)

    if not editing_checkpoint_passed(text, StageType.FIRST_VERIFICATION):
        raise ValidationError(
            "Tekst można przekazać autorowi dopiero po "
            "zakończeniu pierwszej weryfikacji."
        )

    editing_stage = get_active_stage(text, StageType.EDITING)

    if editing_stage is None:
        raise ValidationError("Tekst nie znajduje się obecnie u redaktora.")

    if active_stage_exists(text, StageType.AUTHOR_EDITING):
        raise ValidationError("Tekst znajduje się już u autora.")

    _finish_stage_record(editing_stage, transition_date)

    author_stage = _create_pending_stage(text, StageType.AUTHOR_EDITING)
    return _start_stage(author_stage, transition_date)


@_locked_text_operation
def send_to_second_verification(text, user, started_at=None):
    ensure_editor_access(text, user)
    ensure_text_is_not_withdrawn(text)
    _ensure_editing_phase(text)

    transition_date = _transition_date(started_at)

    if not editing_checkpoint_passed(text, StageType.FIRST_VERIFICATION):
        raise ValidationError(
            "Najpierw należy zakończyć pierwszą weryfikację."
        )

    if current_stage_queryset(text).filter(
        stage_type=StageType.SECOND_VERIFICATION,
    ).exists():
        raise ValidationError(
            "Druga weryfikacja została już utworzona lub zakończona."
        )

    editing_stage = get_active_stage(text, StageType.EDITING)

    if editing_stage is None:
        raise ValidationError(
            "Przed przekazaniem tekst musi znajdować się u redaktora."
        )

    _finish_stage_record(editing_stage, transition_date)
    return _create_pending_stage(text, StageType.SECOND_VERIFICATION)


@_locked_text_operation
def finish_editing_to_coordinator(text, user, ended_at=None):
    ensure_editor_access(text, user)
    ensure_text_is_not_withdrawn(text)
    _ensure_editing_phase(text)

    transition_date = _transition_date(ended_at)

    if not editing_checkpoint_passed(text, StageType.SECOND_VERIFICATION):
        raise ValidationError(
            "Redakcję można zakończyć dopiero po zakończeniu "
            "drugiej weryfikacji."
        )

    editing_stage = get_active_stage(text, StageType.EDITING)

    if editing_stage is None:
        raise ValidationError("Nie ma aktywnej redakcji do zakończenia.")

    _finish_stage_record(editing_stage, transition_date)
    return _create_pending_stage(text, StageType.EDITING_CONTROL)


def user_can_complete_stage(stage, user):
    if not _actor_is_active(user):
        return False

    if not stage_belongs_to_current_cycle(stage):
        return False

    if (
        stage.is_completed
        or stage.started_at is None
        or stage.started_at > timezone.localdate()
        or stage.ended_at is not None
        or text_is_withdrawn(stage.text)
    ):
        return False

    role = STAGE_ROLES.get(stage.stage_type)

    if role is None:
        return False

    if stage.stage_type in (StageType.EDITING, StageType.AUTHOR_EDITING) and not stage.repetition_id:
        return False

    # Kontrola redaktora wymaga wykonania przez przypisanego redaktora.
    if stage.stage_type == StageType.EDITOR_CONTROL:
        return current_assignment_queryset(stage.text).filter(
            role=Role.EDITOR,
            assigned_to_id=user.pk,
        ).exists()

    if stage.stage_type == StageType.STYLING and not user.is_superuser:
        return False

    if is_coordinator(user):
        return True

    return current_assignment_queryset(stage.text).filter(
        role=role,
        assigned_to_id=user.pk,
    ).exists()


@_locked_stage_operation
def complete_stage(stage, user, ended_at):
    _ensure_actor(user)
    if stage.repetition_id:
        from workflow.repetitions import complete_repeat
        return complete_repeat(stage, user, ended_at)
    ensure_text_is_not_withdrawn(stage.text)

    if stage.stage_type == StageType.EDITING:
        raise ValidationError(
            "Redakcję zakończ przez przekazanie tekstu "
            "do weryfikacji, autora albo koordynatora."
        )

    if stage.stage_type == StageType.AUTHOR_EDITING:
        raise ValidationError(
            "Etap u autora zakończ przez wznowienie redakcji."
        )

    if stage.is_completed:
        raise ValidationError("Ten etap został już zakończony.")

    if not user_can_complete_stage(stage, user):
        raise PermissionDenied("Nie możesz zakończyć tego etapu.")

    next_stage_type = NEXT_STAGE_TYPES.get(stage.stage_type)

    if next_stage_type is None:
        raise ValidationError("Ten etap nie ma przejścia do zakończenia.")

    if next_stage_type == StageType.EDITOR_CONTROL:
        editor_assignment = current_assignment_queryset(stage.text).select_related('assigned_to__person_profile').filter(role=Role.EDITOR).first()
        editor = editor_assignment.assigned_to if editor_assignment else None
        if not _actor_is_active(editor) or not (is_coordinator(editor) or belongs_to_group(editor, 'Redaktor')):
            raise ValidationError("Przed zakończeniem kontroli koordynatora przypisz aktywnego redaktora z odpowiednią rolą. Etap nie został zakończony.")
    _finish_stage_record(stage, ended_at)
    next_stage = _create_pending_stage(stage.text, next_stage_type)

    if next_stage_type == StageType.READY:
        _start_stage(next_stage, ended_at)

    return stage


# Zachowana nazwa używana przez dotychczasowe widoki.
start_author_editing = send_text_to_author