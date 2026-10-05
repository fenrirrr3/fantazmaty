"""Real availability and performer changes used by the inactivity report."""
from django.db.models import Case, When, Value, DateField, CharField, ExpressionWrapper, F, Q, Exists, OuterRef, Subquery
from django.utils import timezone
from django.db.models.functions import Cast
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


def later_date(left, right):
    """Null-safe maximum, identical on SQLite and MySQL."""
    return Case(
        When(**{left + '__isnull': True}, then=F(right)),
        When(**{right + '__isnull': True}, then=F(left)),
        When(**{left + '__gte': F(right)}, then=F(left)),
        default=F(right), output_field=DateField(),
    )


def annotate_inactivity_clocks(query, today):
    from workflow.state import ORDER
    from workflow.read_queries import exclude_obsolete_verification_placeholders
    from django.db.models import IntegerField

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

    order = Case(*[When(stage_type=kind, then=Value(index)) for kind,index in ORDER.items()],
                 default=Value(-1), output_field=IntegerField())
    query = query.annotate(_clock_order=order)
    opened = exclude_obsolete_verification_placeholders(same.filter(
        is_current=True, is_released=True, is_completed=False, ended_at__isnull=True,
    ).exclude(pk=OuterRef('pk')))
    earlier = opened.annotate(_clock_order=order).filter(_clock_order__lte=OuterRef('_clock_order'))
    repeat_blockers = same.filter(repetition_id=OuterRef('repetition_id'),
                                 queue_position__lt=OuterRef('queue_position'), is_completed=False)
    query = query.annotate(
        report_blocked=Case(
            When(repetition__isnull=False, then=Exists(repeat_blockers)),
            When(stage_type='first_verification', then=Exists(opened.filter(
                stage_type__in=('editing', 'author_editing')))),
            When(stage_type='editing', then=Exists(opened.filter(
                stage_type__in=('first_verification', 'second_verification', 'author_editing'),
                started_at__isnull=False))),
            default=Exists(earlier),
        ),
        _clock_has_previous=Case(When(repetition__isnull=False, then=Exists(repeated_previous)),
                                 default=Exists(normal_previous)),
        _clock_previous_date=Case(
            When(repetition__isnull=False, then=Subquery(repeated_previous.values('ended_at')[:1])),
            default=Subquery(normal_previous.values('ended_at')[:1]), output_field=DateField()),
    ).annotate(_clock_available=later_date('queued_at', '_clock_previous_date'))
    query = query.annotate(_clock_after_change=later_date('_clock_available', 'waiting_reset_at'))
    return query.annotate(
        report_waiting_since=Cast(Case(
            When(report_blocked=True, then=Value(today)),
            # Never infer the missing completion date from an early reservation.
            When(_clock_has_previous=True, _clock_previous_date__isnull=True, waiting_reset_at__isnull=True,
                 then=Value(None, output_field=DateField())),
            default=F('_clock_after_change'), output_field=DateField()), DateField()),
        # MySQL can infer a string result for CASE mixing dates and parameters.
        # output_field alone does not cast the database result to a DATE.
        report_active_since=Cast(later_date('started_at', 'waiting_reset_at'), DateField()),
    )
