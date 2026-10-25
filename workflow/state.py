"""Process state: semantic stage order first, dates only distinguish planned work."""
from workflow.catalog import active_stage_choices
from django.db.models import Case, When, Value, IntegerField
from django.utils import timezone

ORDER = {value: index for index, (value, _) in enumerate(active_stage_choices())}

def state_key(stage):
    kind = stage.stage_type
    if kind == 'withdrawn':
        category = 0
    elif kind == "ready":
        category = 1
    elif kind == "author_editing":
        category = 2
    elif stage.started_at and stage.started_at <= timezone.localdate():
        category = 3
    elif stage.started_at:
        category = 4
    else:
        category = 5
    return category, -ORDER.get(kind, -1), -stage.iteration, -stage.pk


def current_stage(stages):
    stages = [s for s in stages if s.is_current and s.is_released and s.stage_type in ORDER]
    opened = [s for s in stages if not s.is_completed and s.ended_at is None]
    if opened:
        return min(opened, key=state_key)
    # A correction can remove the pending continuation. Report the last
    # completed checkpoint without reopening it or inventing a new execution.
    return max(stages, key=lambda s: (ORDER[s.stage_type], s.iteration, s.pk), default=None)


def current_stage_subquery(stages, field):
    """SQL odpowiednik current_stage(): pole `field` otwartego etapu o najwyższym priorytecie,
    a gdy otwartych brak – ostatniego wg kolejności procesu.

    `stages` – etapy bieżącego cyklu skorelowane z tekstem (OuterRef).
    """
    from django.db.models import Subquery
    from django.db.models.functions import Coalesce
    candidates = stages.filter(is_released=True, stage_type__in=ORDER).annotate(**state_annotations())
    return Coalesce(
        Subquery(candidates.filter(is_completed=False, ended_at__isnull=True)
                 .order_by('state_priority', '-state_order', '-iteration', '-pk').values(field)[:1]),
        Subquery(candidates.order_by('-state_order', '-iteration', '-pk').values(field)[:1]),
    )


def stage_order(default=-1, field='stage_type'):
    """Pozycja etapu w katalogu procesu (ORDER) jako wyrażenie SQL; etap spoza katalogu – default.

    ``field`` – pole z rodzajem etapu (np. adnotacja ``current_stage_type`` listy tekstów)."""
    return Case(
        *[When(**{field: kind}, then=Value(index)) for kind, index in ORDER.items()],
        default=Value(default),
        output_field=IntegerField(),
    )


def state_annotations():
    return {
        "state_priority": Case(
            When(stage_type="withdrawn", then=Value(0)),
            When(stage_type="ready", then=Value(1)),
            When(stage_type="author_editing", then=Value(2)),
            When(started_at__lte=timezone.localdate(), then=Value(3)),
            When(started_at__isnull=False, then=Value(4)),
            default=Value(5),
            output_field=IntegerField(),
        ),
        "state_order": stage_order(),
    }


def stage_is_open(stage):
    return not stage.is_completed and stage.ended_at is None


def stage_is_active(stage, today):
    return (
        stage.is_current
        and stage.is_released
        and stage_is_open(stage)
        and stage.started_at is not None
        and stage.started_at <= today
    )
