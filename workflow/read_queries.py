"""Database predicates for read-only workflow lists; writes use workflow services."""
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
    return S.objects.filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))


def open_stages(query):
    return query.filter(is_completed=False, ended_at__isnull=True)


def active_stages(query, today):
    return open_stages(query).filter(started_at__lte=today)


def terminal(query):
    return query.filter(stage_type__in=('ready', 'withdrawn'))


def own_stages(user):
    assigned = A.objects.filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'),
                                assigned_to_id=user.pk, role=OuterRef('_work_role'))
    return S.objects.alias(_work_role=stage_role()).filter(Exists(assigned)).exclude(
        stage_type__in=('author_editing', 'ready_for_editing'))


def waiting_editor(query, today):
    return (Exists(open_stages(query).filter(stage_type__in=('first_verification', 'second_verification', 'author_editing')))
            & ~Exists(active_stages(query, today).filter(stage_type='editing')))


def reserved_assignments(user, today):
    stages = current_stages()
    matching = stages.alias(_work_role=stage_role()).filter(_work_role=OuterRef('role')).exclude(
        stage_type__in=('author_editing', 'ready_for_editing'))
    return A.objects.filter(assigned_to_id=user.pk).filter(
        ~Exists(matching) | (~Exists(active_stages(matching, today)) & Exists(open_stages(matching)))
    ).exclude(Q(role=A.Role.EDITOR) & waiting_editor(stages, today))


def filter_my_texts(queryset, user, selected_view, today):
    if selected_view == 'all':
        return queryset
    stages = S.objects.filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'))
    own = own_stages(user).filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'))
    assigned = A.objects.filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'), assigned_to_id=user.pk)
    reserved = reserved_assignments(user, today).filter(text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'))
    waiting = Exists(assigned.filter(role=A.Role.EDITOR)) & waiting_editor(stages, today)
    closed = Exists(terminal(stages))
    active = Exists(active_stages(own, today))
    pending = Exists(reserved) | waiting
    if selected_view == 'active':
        return queryset.filter(~closed & active)
    if selected_view == 'waiting':
        return queryset.filter(~closed & pending)
    return queryset.filter(Exists(own) & ~Exists(own.filter(is_completed=False)) & (closed | (~active & ~pending)))


def available_stages(user, access):
    stages = current_stages()
    assignments = A.objects.filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))
    query = S.objects.filter(workflow_cycle=F('text__current_workflow_cycle'), is_completed=False,
                             started_at__isnull=True, ended_at__isnull=True).alias(_work_role=stage_role())
    if not access['member']:
        return query.none()
    kinds = [kind for kind, role in {'ready_for_editing': A.Role.EDITOR, **STAGE_ROLES}.items()
             if (kind != 'styling' or user.is_superuser)
             and (access['coordinator'] or ROLE_GROUPS.get(role, 'Redaktor').casefold() in access['roles'])]
    query = query.filter(stage_type__in=kinds).exclude(stage_type='editor_control').filter(
        ~Exists(terminal(stages)),
        ~Exists(assignments.filter(role=OuterRef('_work_role'), assigned_to__isnull=False)),
    )
    opposite = Case(When(_work_role='verifier_1', then=Value('verifier_2')),
                    When(_work_role='verifier_2', then=Value('verifier_1')), default=Value(''), output_field=CharField())
    query = query.alias(_opposite=opposite).filter(~Exists(assignments.filter(role=OuterRef('_opposite'), assigned_to_id=user.pk)))
    entry = ~Exists(stages.exclude(pk=OuterRef('pk')))
    query = query.filter(~Q(stage_type__in=('editing', 'author_editing')) | entry)
    query = query.filter(~Q(stage_type='first_verification') | entry | Exists(
        open_stages(stages).filter(stage_type='editing', started_at__isnull=False)))
    query = query.filter(~Q(stage_type='second_verification') | entry | (
        Exists(stages.filter(stage_type='first_verification', is_completed=True))
        & ~Exists(open_stages(stages).filter(stage_type__in=('editing', 'author_editing')))))
    return query.order_by('text__anthology__title', 'text__title', 'text_id', '-pk')


def dashboard_querysets(user, today):
    stages = current_stages()
    active = active_stages(own_stages(user), today).filter(
        workflow_cycle=F('text__current_workflow_cycle')).filter(~Exists(terminal(stages))).order_by('started_at', 'pk')
    reserved = reserved_assignments(user, today).filter(workflow_cycle=F('text__current_workflow_cycle')).filter(
        ~Exists(terminal(stages))).order_by('text__anthology__title', 'text__title', 'text_id', 'role', 'pk')
    return active, reserved
