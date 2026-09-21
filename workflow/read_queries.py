"""Database predicates for read-only workflow lists; writes use workflow services."""
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES
from django.db.models import Case, When, Value, CharField, Exists, OuterRef, Q, F
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import STAGE_ROLES, ROLE_GROUPS


def stage_role():
    return Case(
        When(stage_type='ready_for_editing', then=Value(A.Role.EDITOR)),
        *[When(stage_type=kind, then=Value(role)) for kind, role in STAGE_ROLES.items()],
        default=Value(''), output_field=CharField(),
    )


def current_stages():
    return S.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))


def open_stages(query):
    return query.filter(is_completed=False, ended_at__isnull=True)


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
            & ~Exists(active_stages(query, today).filter(stage_type='editing')))


def reserved_assignments(user, today):
    stages = current_stages()
    matching = stages.alias(_work_role=stage_role()).filter(Q(assignment_id=OuterRef('pk')) | Q(assignment__isnull=True, _work_role=OuterRef('role'))).exclude(
        stage_type__in=('author_editing', 'ready_for_editing'))
    return A.objects.current_cycle().filter(assigned_to_id=user.pk).filter(
        ~Exists(matching) | (~Exists(active_stages(matching, today)) & Exists(open_stages(matching)))
    ).exclude(Q(role=A.Role.EDITOR) & waiting_editor(stages, today))


def annotate_my_work(queryset, user, today):
    stages = S.objects.current_cycle().filter(text_id=OuterRef('pk'))
    own = own_stages(user).filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'))
    assigned = A.objects.current_cycle().filter(text_id=OuterRef('pk'), assigned_to_id=user.pk)
    reserved = reserved_assignments(user, today).filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'), is_current=True)
    closed = Exists(terminal(stages))
    active = Exists(active_stages(own.filter(is_current=True), today)) & ~closed
    # Redakcja jest gotowa dopiero po zakończonej kontroli koordynatora redakcji.
    # Brak dat nie ma znaczenia: liczy się jawny stan zakończenia, również przy imporcie.
    editorial_work = Exists(assigned.filter(role=A.Role.EDITOR)) | Exists(own.filter(_work_role=A.Role.EDITOR))
    # The imported terminal status confirms completed editorial work even if
    # the source omitted a coordinator check. No fictional check is created.
    # Repeating editing retires these stages and removes READY, so this does not
    # approve any later work automatically.
    imported_editing_approved = (Exists(stages.filter(stage_type=S.StageType.READY))
        & Exists(stages.filter(stage_type=S.StageType.EDITING, imported_completed=True, is_completed=True)))
    editing_approved = Exists(stages.filter(stage_type=S.StageType.EDITING_CONTROL, is_completed=True)) | imported_editing_approved
    editor_remaining = editorial_work & ~editing_approved
    live_editor_waiting = editor_remaining & Exists(assigned.filter(role=A.Role.EDITOR).exclude(stages__imported_completed=True))
    pending = ~active & ~closed & (Exists(own.filter(is_current=True, is_completed=False)) | Exists(reserved) | live_editor_waiting)
    completed = Exists(own.filter(is_completed=True)) & ~Exists(own.filter(is_current=True, is_completed=False)) & ~active & ~pending & ~editor_remaining
    return queryset.annotate(work_active=active, work_waiting=pending, work_completed=completed)


def filter_my_texts(queryset, user, selected_view, today):
    queryset = annotate_my_work(queryset, user, today)
    field = {'active':'work_active', 'waiting':'work_waiting', 'completed':'work_completed'}.get(selected_view)
    return queryset.filter(**{field:True}) if field else queryset


def available_stages(user, access):
    stages = current_stages()
    assignments = A.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))
    query = S.objects.current_cycle().exclude(text__anthology__status="ready").filter(is_released=True, workflow_cycle=F('text__current_workflow_cycle'), is_completed=False,
                             started_at__isnull=True, ended_at__isnull=True).alias(_work_role=stage_role())
    if not access['member']:
        return query.none()
    kinds = [kind for kind, role in {'ready_for_editing': A.Role.EDITOR, **STAGE_ROLES}.items()
             if (kind != 'styling' or user.is_superuser)
             and (access['coordinator'] or ROLE_GROUPS.get(role, 'Redaktor').casefold() in access['roles'])]
    query = query.filter(stage_type__in=kinds).exclude(stage_type='editor_control', repetition__isnull=True).filter(
        ~Exists(terminal(stages)),
        ~Exists(assignments.filter(role=OuterRef('_work_role'), assigned_to__isnull=False)),
    )
    opposite = Case(When(_work_role='verifier_1', then=Value('verifier_2')),
                    When(_work_role='verifier_2', then=Value('verifier_1')), default=Value(''), output_field=CharField())
    query = query.alias(_opposite=opposite).filter(~Exists(assignments.filter(role=OuterRef('_opposite'), assigned_to_id=user.pk)))
    entry = Q(repetition__isnull=False) | Q(~Exists(stages.exclude(pk=OuterRef('pk'))))
    query = query.filter(~Q(stage_type__in=('editing', 'author_editing')) | entry)
    query = query.filter(~Q(stage_type='first_verification') | entry | Exists(
        stages.filter(stage_type='editing', started_at__isnull=False)))
    query = query.filter(~Q(stage_type='second_verification') | entry | (
        Exists(stages.filter(stage_type='first_verification', is_completed=True))
        & ~Exists(open_stages(stages).filter(stage_type__in=('editing', 'author_editing')))))
    return query.order_by('text__anthology__title', 'text__title', 'text_id', '-pk')


def dashboard_querysets(user, today):
    from texts.models import Text
    classified = annotate_my_work(Text.objects.all(), user, today)
    active_texts = classified.filter(work_active=True).values('pk')
    waiting_texts = classified.filter(work_waiting=True).values('pk')
    stages = current_stages()
    active = active_stages(own_stages(user), today).filter(text_id__in=active_texts).filter(
        workflow_cycle=F('text__current_workflow_cycle'), is_current=True).filter(~Exists(terminal(stages))).order_by(F('started_at').desc(nulls_last=True), '-pk')
    reserved = reserved_assignments(user, today).filter(text_id__in=waiting_texts).filter(workflow_cycle=F('text__current_workflow_cycle')).filter(
        ~Exists(terminal(stages))).order_by(F('assigned_at').desc(nulls_last=True), '-pk')
    return active, reserved


def work_text_ids(queryset, user, today):
    """The same three categories used by My texts, profile and dashboard."""
    result = {'active': set(), 'waiting': set(), 'completed': set()}
    for pk, active, waiting, completed in annotate_my_work(queryset, user, today).values_list(
        'pk', 'work_active', 'work_waiting', 'work_completed'
    ):
        for name, flag in (('active', active), ('waiting', waiting), ('completed', completed)):
            if flag:
                result[name].add(pk)
    return result
