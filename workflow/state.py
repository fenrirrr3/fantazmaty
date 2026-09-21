"""Process state: semantic stage order first, dates only distinguish planned work."""
from django.db.models import Case, When, Value, IntegerField
from django.utils import timezone
from workflow.models import WorkflowStage

ORDER = {value: index for index, (value, _) in enumerate(WorkflowStage.StageType.choices)}

def state_key(stage):
    kind = stage.stage_type
    if kind == 'withdrawn': category = 0
    elif kind == 'ready': category = 1
    elif kind == 'author_editing': category = 2
    elif stage.started_at and stage.started_at <= timezone.localdate(): category = 3
    elif stage.started_at: category = 4
    else: category = 5
    return category, -ORDER.get(kind, -1), -stage.iteration, -stage.pk

def current_stage(stages):
    opened = [s for s in stages if s.is_current and s.is_released and not s.is_completed and s.ended_at is None]
    return min(opened, key=state_key) if opened else None

def state_annotations():
    return {
        'state_priority': Case(
            When(stage_type='withdrawn',then=Value(0)),
            When(stage_type='ready',then=Value(1)),
            When(stage_type='author_editing',then=Value(2)),
            When(started_at__lte=timezone.localdate(),then=Value(3)),
            When(started_at__isnull=False,then=Value(4)),default=Value(5),output_field=IntegerField()),
        'state_order': Case(*[When(stage_type=kind,then=Value(index)) for kind,index in ORDER.items()],default=Value(-1),output_field=IntegerField()),
    }




def stage_is_open(stage):
    return not stage.is_completed and stage.ended_at is None


def stage_is_active(stage, today):
    return (stage.is_current and stage.is_released and stage_is_open(stage)
            and stage.started_at is not None and stage.started_at <= today)
