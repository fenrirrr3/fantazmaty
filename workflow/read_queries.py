"""Database predicates for read-only workflow lists; writes use workflow services."""
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES
from django.db.models import Case, When, Value, CharField, Exists, OuterRef, Q, F, Subquery
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import STAGE_ROLES, ROLE_GROUPS


def stage_role():
    kinds = {}
    for kind, role in {'ready_for_editing': A.Role.EDITOR, **STAGE_ROLES}.items():
        kinds.setdefault(role, []).append(kind)
    return Case(
        *[When(stage_type__in=values, then=Value(role)) for role, values in kinds.items()],
        default=Value(''), output_field=CharField(),
    )


def current_stages():
    return S.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))


def completed_verification_history():
    """Correlated history, including non-current executions in the same cycle."""
    return S.objects.filter(
        text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'),
        stage_type=OuterRef('stage_type'), is_completed=True, is_skipped=False,
    ).exclude(repetition__canceled_at__isnull=False)


def exclude_obsolete_verification_placeholders(query):
    completed = completed_verification_history()
    return query.exclude(
        Q(is_current=True, repetition__isnull=True, is_completed=False,
          stage_type__in=('first_verification', 'second_verification'),
          started_at__isnull=True, ended_at__isnull=True) & Q(Exists(completed))
    )


def unfinished(query):
    """Open stages without the placeholder check; use only when verification stages are filtered out."""
    return query.filter(is_completed=False, ended_at__isnull=True)


def open_stages(query):
    return exclude_obsolete_verification_placeholders(unfinished(query))


def active_stages(query, today):
    return open_stages(query).filter(is_released=True, started_at__lte=today)


def terminal(query):
    return query.filter(stage_type__in=('ready', 'withdrawn'))


def own_stages(user):
    assigned = A.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'),
                                assigned_to_id=user.pk, role=OuterRef('_work_role'))
    return S.objects.alias(_work_role=stage_role()).filter(
        Q(assignment__assigned_to_id=user.pk) | (Q(assignment__isnull=True, is_current=True) & Q(Exists(assigned)))
    ).exclude(stage_type__in=('author_editing', 'ready_for_editing', *IMPORT_ONLY_STAGE_TYPES))


def waiting_editor(query, today):
    return (Exists(open_stages(query).filter(stage_type__in=('first_verification', 'second_verification', 'author_editing')))
            & ~Exists(unfinished(query).filter(stage_type='editing', is_released=True, started_at__lte=today)))


def reserved_assignments(user, today):
    stages = current_stages()
    matching = stages.alias(_work_role=stage_role()).filter(Q(assignment_id=OuterRef('pk')) | Q(assignment__isnull=True, _work_role=OuterRef('role'))).exclude(
        stage_type__in=('author_editing', 'ready_for_editing'))
    performed = S.objects.filter(
        assignment_id=OuterRef('pk'), text_id=OuterRef('text_id'),
        workflow_cycle=OuterRef('workflow_cycle'), is_completed=True, is_skipped=False,
    ).exclude(repetition__canceled_at__isnull=False)
    return A.objects.current_cycle().filter(assigned_to_id=user.pk).filter(
        # Restoring a team member does not reserve their already performed work.
        (~Exists(matching) & ~Exists(performed))
        | (~Exists(active_stages(matching, today)) & Exists(open_stages(matching)))
    ).exclude(Q(role=A.Role.EDITOR) & waiting_editor(stages, today))


def editorial_approval(stages, today):
    """Editing ends after completed coordinator control and handoff to K1."""
    proofreading = Exists(stages.filter(
        stage_type=S.StageType.FIRST_PROOFREADING, is_released=True,
        is_skipped=False,
    ).exclude(repetition__canceled_at__isnull=False))
    # A prior proofreading pass cannot approve a new editorial repetition.
    editorial_repeat = Exists(stages.filter(
        repetition__isnull=False, repetition__completed_at__isnull=True,
        repetition__canceled_at__isnull=True, is_completed=False,
        stage_type__in=('editing', 'author_editing', 'first_verification', 'second_verification'),
    ))
    # Existing Ready texts keep their classification even with incomplete history;
    # imported Ready texts whose historical dates are unknown stay approved.
    ready = Exists(stages.filter(stage_type=S.StageType.READY))
    imported_editing = Exists(stages.filter(stage_type=S.StageType.EDITING, imported_completed=True, is_completed=True))
    legacy_approval = (proofreading & ~editorial_repeat) | imported_editing
    control = stages.filter(stage_type=S.StageType.EDITING_CONTROL, is_completed=True,
                            is_skipped=False).exclude(send_to_proofreading=False)
    returned_to_editor = stages.filter(stage_type=S.StageType.EDITING, is_completed=False,
                                       ended_at__isnull=True, is_released=True)
    return ((ready & legacy_approval) | (~ready & proofreading
            & Exists(control) & ~editorial_repeat & ~Exists(returned_to_editor)))


def annotate_my_work(queryset, user, today):
    """Work state of the user's texts: active, waiting or completed (mutually exclusive).

    Each subquery is built once and the flags are written in a simplified,
    equivalent form (every term is a non-null EXISTS):
      active    = own_active & ~closed
      waiting   = ~own_active & ~closed & (pending_own | reserved | editor waits for approval)
      completed = own_completed & ~pending_own & ~editor_remaining & (closed | ~own_active & ~reserved)
    Repeating whole flags inside each other used to multiply the SQL (and the
    cost of building it) several times over.
    """
    stages = S.objects.current_cycle().filter(text_id=OuterRef('pk'))
    own = own_stages(user).filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'))
    pending_own = Exists(open_stages(own.filter(is_current=True)))
    assigned = A.objects.current_cycle().filter(text_id=OuterRef('pk'), assigned_to_id=user.pk)
    editor_assigned = Exists(assigned.filter(role=A.Role.EDITOR))
    reserved = Exists(reserved_assignments(user, today).filter(
        text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'), is_current=True))
    closed = Exists(terminal(stages))
    # Praca redaktora pozostaje w toku podczas poprawek autora. To klasyfikacja
    # tekstu, nie przypisanie redaktorowi etapu wykonywanego przez autora.
    with_author = editor_assigned & Exists(unfinished(stages).filter(
        stage_type=S.StageType.AUTHOR_EDITING, is_released=True,
    ))
    own_active = Exists(active_stages(own.filter(is_current=True), today)) | with_author
    approved = editorial_approval(stages, today)
    editor_remaining = (editor_assigned | Exists(own.filter(_work_role=A.Role.EDITOR))) & ~approved
    # The editor returns only for their own control, covered by pending_own
    # and own_active; the coordinator's control is not the editor's task.
    # An assigned editor waits until editorial approval (editor_remaining & editor_assigned).
    active = own_active & ~closed
    pending = ~own_active & ~closed & (pending_own | reserved | (editor_assigned & ~approved))
    completed = (Exists(own.filter(is_completed=True)) & ~pending_own & ~editor_remaining
                 & (closed | (~own_active & ~reserved)))
    return queryset.annotate(work_active=active, work_waiting=pending, work_completed=completed)


def available_stages(user, access):
    from workflow.availability import can_claim_fourth_proofreading, first_proofreading_work
    stages = current_stages()
    checkpoints = S.objects.filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'),
                                    is_completed=True, is_skipped=False).exclude(repetition__canceled_at__isnull=False)
    assignments = A.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))
    query = S.objects.current_cycle().exclude(text__anthology__status__in=("ready", "abandoned")).filter(is_released=True, workflow_cycle=F('text__current_workflow_cycle'), is_completed=False,
                             started_at__isnull=True, ended_at__isnull=True).alias(_work_role=stage_role())
    if not access['member']:
        return query.none()
    kinds = [kind for kind, role in {'ready_for_editing': A.Role.EDITOR, **STAGE_ROLES}.items()
             if (kind != 'styling' or user.is_superuser)
             and (kind not in ('second_proofreading', 'fourth_proofreading') or can_claim_fourth_proofreading(user))
             and (access['coordinator'] or ROLE_GROUPS.get(role, 'Redaktor').casefold() in access['roles'])]
    query = query.filter(stage_type__in=kinds).exclude(stage_type='editor_control', repetition__isnull=True).filter(
        ~Exists(terminal(stages)),
        ~Exists(assignments.filter(role=OuterRef('_work_role'), assigned_to__isnull=False)),
    )
    query = query.filter(~Q(stage_type__in=('second_proofreading', 'third_proofreading')) | ~Exists(first_proofreading_work(user).filter(text_id=OuterRef('text_id'))))
    opposite = Case(When(_work_role='verifier_1', then=Value('verifier_2')),
                    When(_work_role='verifier_2', then=Value('verifier_1')), default=Value(''), output_field=CharField())
    query = query.alias(_opposite=opposite).filter(~Exists(assignments.filter(role=OuterRef('_opposite'), assigned_to_id=user.pk)))
    query = query.filter(
        ~Q(stage_type__in=('first_verification', 'second_verification'))
        | Q(repetition__isnull=False)
        | ~Exists(checkpoints.filter(stage_type=OuterRef('stage_type')))
    )
    entry = Q(repetition__isnull=False) | Q(~Exists(stages.exclude(pk=OuterRef('pk'))))
    query = query.filter(~Q(stage_type__in=('editing', 'author_editing')) | entry)
    query = query.filter(~Q(stage_type='first_verification') | entry | Exists(
        stages.filter(stage_type='editing').filter(Q(started_at__isnull=False) | Q(is_completed=True))))
    query = query.filter(~Q(stage_type='second_verification') | entry | (
        Exists(checkpoints.filter(stage_type='first_verification'))
        & ~Exists(open_stages(stages).filter(stage_type__in=('editing', 'author_editing')))))
    return query.annotate(reservation_only=(
        Q(stage_type='first_verification', repetition__isnull=True)
        & Exists(stages.filter(stage_type__in=('editing', 'author_editing'),
                               is_completed=False, ended_at__isnull=True))
    )).order_by('text__anthology__title', 'text__title', 'text_id', '-pk')


def user_text_ids(user):
    """Texts where the user holds any role or stage assignment (all cycles).

    Every work state in annotate_my_work requires such an assignment, so this
    narrows the expensive classification to the user's own texts.
    """
    ids = set(A.objects.filter(assigned_to_id=user.pk).values_list('text_id', flat=True))
    ids.update(S.objects.filter(assignment__assigned_to_id=user.pk).values_list('text_id', flat=True))
    return ids


def classify_user_texts(user, today):
    """Work state of the user's texts in one query: active, waiting and waiting with editor role."""
    from texts.models import Text
    candidates = user_text_ids(user)
    if not candidates:
        return [], [], []
    editor = A.objects.current_cycle().filter(text_id=OuterRef('pk'), role=A.Role.EDITOR, assigned_to_id=user.pk)
    rows = list(annotate_my_work(Text.objects.filter(pk__in=candidates), user, today)
                .annotate(editor_assigned=Exists(editor))
                .values_list('pk', 'work_active', 'work_waiting', 'editor_assigned'))
    active = [pk for pk, is_active, _, _ in rows if is_active]
    waiting = [pk for pk, _, is_waiting, _ in rows if is_waiting]
    editor_waiting = [pk for pk, _, is_waiting, is_editor in rows if is_waiting and is_editor]
    return active, waiting, editor_waiting


def dashboard_querysets(user, today):
    # Klasyfikacja tekstów jest liczona raz, tylko dla tekstów użytkownika;
    # dalsze zapytania dostają gotowe listy identyfikatorów zamiast podzapytań.
    active_texts, waiting_texts, editor_waiting_texts = classify_user_texts(user, today)
    stages = current_stages()
    editor = A.objects.current_cycle().filter(text_id=OuterRef('text_id'),
        workflow_cycle=OuterRef('workflow_cycle'), role=A.Role.EDITOR, assigned_to_id=user.pk)
    dashboard_work = S.objects.filter(
        Q(pk__in=own_stages(user).values('pk')) |
        (Q(stage_type=S.StageType.AUTHOR_EDITING) & Q(Exists(editor))))
    active = active_stages(dashboard_work, today).filter(text_id__in=active_texts).filter(
        workflow_cycle=F('text__current_workflow_cycle'), is_current=True).filter(~Exists(terminal(stages)))
    from workflow.state import state_annotations, ORDER
    from django.db.models.functions import Coalesce
    candidates = exclude_obsolete_verification_placeholders(stages.filter(
        is_released=True, stage_type__in=ORDER,
    )).annotate(**state_annotations())
    representative = Coalesce(
        Subquery(candidates.filter(is_completed=False, ended_at__isnull=True)
                 .order_by('state_priority', '-state_order', '-iteration', '-pk').values('pk')[:1]),
        Subquery(candidates.order_by('-state_order', '-iteration', '-pk').values('pk')[:1]),
    )
    editorial_waiting_ids = list(S.objects.current_cycle().filter(text_id__in=editor_waiting_texts,
        is_released=True, pk=representative).filter(~Exists(terminal(stages))).values_list('pk', flat=True)) \
        if editor_waiting_texts else []
    active_ids = list(active.values_list('pk', flat=True)) if active_texts else []
    active = S.objects.filter(pk__in=[*active_ids, *editorial_waiting_ids]).annotate(
        editor_waiting=Q(pk__in=editorial_waiting_ids)
            | (Q(stage_type=S.StageType.AUTHOR_EDITING) & Q(Exists(editor))),
    ).order_by(F('started_at').desc(nulls_last=True), '-pk')
    reserved = (reserved_assignments(user, today).filter(text_id__in=waiting_texts)
        .filter(workflow_cycle=F('text__current_workflow_cycle')).filter(~Exists(terminal(stages)))
        .exclude(Q(role=A.Role.EDITOR) & Q(text_id__in=editor_waiting_texts))
        .order_by(F('assigned_at').desc(nulls_last=True), '-pk'))
    if waiting_texts:
        reserved_ids = list(reserved.values_list('pk', flat=True))
        reserved = A.objects.filter(pk__in=reserved_ids).order_by(F('assigned_at').desc(nulls_last=True), '-pk')
    else:
        reserved = A.objects.none()
    return active, reserved


def work_text_ids(queryset, user, today):
    """Internal work states shared by row badges, profile and dashboard."""
    result = {'active': set(), 'waiting': set(), 'completed': set()}
    for pk, active, waiting, completed in annotate_my_work(queryset, user, today).values_list(
        'pk', 'work_active', 'work_waiting', 'work_completed'
    ):
        for name, flag in (('active', active), ('waiting', waiting), ('completed', completed)):
            if flag:
                result[name].add(pk)
    return result
