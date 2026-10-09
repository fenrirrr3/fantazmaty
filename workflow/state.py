"""Process state: semantic stage order first, dates only distinguish planned work."""
from workflow.catalog import active_stage_choices
from django.db.models import Case, When, Value, IntegerField
from django.utils import timezone
from workflow.models import WorkflowStage

ORDER = {value: index for index, (value, _) in enumerate(active_stage_choices())}

def operational_stages(stages):
    """Ignore empty legacy verification duplicates; keep performed work intact."""
    stages = list(stages)
    completed = {(s.text_id, s.workflow_cycle, s.stage_type) for s in stages
                 if s.is_completed and not s.is_skipped
                 and (not s.repetition_id or not s.repetition.canceled_at)}
    candidates = [s for s in stages if s.is_current and not s.repetition_id
                  and not s.is_completed and s.stage_type in ('first_verification', 'second_verification')
                  and s.started_at is None and s.ended_at is None]
    completed.update((s.text_id, s.workflow_cycle, s.stage_type) for s in candidates
                     if getattr(s, '_has_completed_verification', False))
    candidates = [s for s in candidates if not hasattr(s, '_has_completed_verification')]
    if candidates:
        completed.update(WorkflowStage.objects.using(candidates[0]._state.db).filter(
            text_id__in={s.text_id for s in candidates},
            workflow_cycle__in={s.workflow_cycle for s in candidates},
            stage_type__in=('first_verification', 'second_verification'),
            is_completed=True, is_skipped=False,
        ).exclude(repetition__canceled_at__isnull=False).values_list('text_id', 'workflow_cycle', 'stage_type'))
    return [s for s in stages if not (
        s.is_current and not s.repetition_id and not s.is_completed
        and s.stage_type in ('first_verification', 'second_verification')
        and s.started_at is None and s.ended_at is None
        and (s.text_id, s.workflow_cycle, s.stage_type) in completed
    )]

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


def current_stage(stages, *, prepared=False):
    stages = stages if prepared else operational_stages(stages)
    stages = [s for s in stages if s.is_current and s.is_released and s.stage_type in ORDER]
    opened = [s for s in stages if not s.is_completed and s.ended_at is None]
    if opened:
        return min(opened, key=state_key)
    # A correction can remove the pending continuation. Report the last
    # completed checkpoint without reopening it or inventing a new execution.
    return max(stages, key=lambda s: (ORDER[s.stage_type], s.iteration, s.pk), default=None)


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
        "state_order": Case(
            *[When(stage_type=kind, then=Value(index)) for kind, index in ORDER.items()],
            default=Value(-1),
            output_field=IntegerField(),
        ),
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
