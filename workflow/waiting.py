"""Real availability and performer changes used by the inactivity report."""
from django.db.models import Case, When, DateField, CharField, ExpressionWrapper, Q, Exists, OuterRef, Subquery
from django.utils import timezone
from workflow.models import WorkflowStage as S


def open_clock_stages(text_id, using):
    closed = S.objects.using(using).current_cycle().filter(
        text_id=OuterRef('text_id'), stage_type__in=('ready', 'withdrawn'))
    return S.objects.using(using).current_cycle().filter(
        text_id=text_id, is_completed=False, ended_at__isnull=True,
    ).exclude(stage_type__in=('ready', 'withdrawn')).filter(~Exists(closed))


def reset_assignment_waiting(assignment, *, using):
    from workflow.import_context import importing_completed
    if importing_completed.get():
        return
    open_clock_stages(assignment.text_id, using).filter(assignment_id=assignment.pk).update(
        waiting_reset_at=timezone.localdate())


def performer_snapshot(text):
    return dict(open_clock_stages(text.pk, text._state.db or 'default').values_list(
        'pk', 'assignment__assigned_to_id'))


def reset_changed_performers(text, before):
    after = performer_snapshot(text)
    changed = [pk for pk, user_id in after.items() if pk in before and before[pk] != user_id]
    if changed:
        using = text._state.db or 'default'
        S.objects.using(using).filter(pk__in=changed).update(waiting_reset_at=timezone.localdate())
        from core.edit_versions import bump
        for pk in changed:
            bump('workflow.workflowstage', pk, using)


def _clock_terms():
    """Elementary terms of the inactivity clocks, correlated with the stage.

    Returns (order, blocked, has_previous, previous_date); `blocked` refers to
    OuterRef('_clock_order'), so the query must annotate `_clock_order=order`.
    """
    from workflow.state import stage_order

    # Only the relevant predecessor can establish availability. An unrelated
    # recently completed task must not erase a real delay.
    predecessors = {
        'first_verification': ('editing',), 'second_verification': ('editing',),
        'author_editing': ('editing',), 'editing_control': ('editing',),
        'first_proofreading': ('editing_control',),
        'second_proofreading': ('first_proofreading',),
        'third_verification': ('second_proofreading',),
        'coordinator_control': ('third_verification',),
        'editor_control': ('coordinator_control',),
        'third_proofreading': ('editor_control',),
        'fourth_proofreading': ('third_proofreading',),
        'styling': ('fourth_proofreading',),
        'editing': ('first_verification', 'second_verification', 'author_editing', 'editing_control'),
    }
    same = S.objects.filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))
    previous = same.filter(is_completed=True).exclude(repetition__canceled_at__isnull=False)
    previous = previous.annotate(_target_kind=ExpressionWrapper(OuterRef('stage_type'), output_field=CharField()))
    matches = Q(pk__in=[])
    for kind, kinds in predecessors.items():
        matches |= Q(_target_kind=kind, stage_type__in=kinds)
    normal_previous = previous.filter(matches).order_by('-pk')
    repeated_previous = previous.filter(
        repetition_id=OuterRef('repetition_id'), queue_position__lt=OuterRef('queue_position'),
    ).order_by('-queue_position', '-pk')

    order = stage_order()
    opened = same.filter(
        is_current=True, is_released=True, is_completed=False, ended_at__isnull=True,
    ).exclude(pk=OuterRef('pk'))
    earlier = opened.annotate(_clock_order=order).filter(_clock_order__lte=OuterRef('_clock_order'))
    repeat_blockers = same.filter(repetition_id=OuterRef('repetition_id'),
                                 queue_position__lt=OuterRef('queue_position'), is_completed=False)
    blocked = Case(
        When(repetition__isnull=False, then=Exists(repeat_blockers)),
        When(stage_type='first_verification', then=Exists(opened.filter(
            stage_type__in=('editing', 'author_editing')))),
        When(stage_type='editing', then=Exists(opened.filter(
            stage_type__in=('first_verification', 'second_verification', 'author_editing'),
            started_at__isnull=False))),
        default=Exists(earlier),
    )
    has_previous = Case(When(repetition__isnull=False, then=Exists(repeated_previous)),
                        default=Exists(normal_previous))
    previous_date = Case(
        When(repetition__isnull=False, then=Subquery(repeated_previous.values('ended_at')[:1])),
        default=Subquery(normal_previous.values('ended_at')[:1]), output_field=DateField())
    return order, blocked, has_previous, previous_date


CLOCK_FIELDS = ('started_at', 'queued_at', 'waiting_reset_at', 'clock_blocked', 'clock_has_previous',
                'clock_previous_date')


def annotate_clock_terms(query):
    """Elementary clock terms, each selected once; inactivity_since() derives the dates from CLOCK_FIELDS."""
    order, blocked, has_previous, previous_date = _clock_terms()
    return query.annotate(_clock_order=order).annotate(
        clock_blocked=blocked, clock_has_previous=has_previous, clock_previous_date=previous_date)


def _later(left, right):
    """Null-safe maximum of two dates (an empty value never wins)."""
    if left is None or right is None:
        return right if left is None else left
    return max(left, right)


def inactivity_since(started_at, queued_at, waiting_reset_at, clock_blocked, clock_has_previous,
                     clock_previous_date, *, today):
    """(waiting since, active since) of a stage from its CLOCK_FIELDS values.

    A blocked stage waits since today; a stage after work with an unknown completion
    date (and without a performer change) has no known waiting date.
    """
    if clock_blocked:
        waiting = today
    elif clock_has_previous and clock_previous_date is None and waiting_reset_at is None:
        waiting = None
    else:
        waiting = _later(_later(queued_at, clock_previous_date), waiting_reset_at)
    return waiting, _later(started_at, waiting_reset_at)
