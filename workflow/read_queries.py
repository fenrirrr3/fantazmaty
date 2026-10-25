"""Database predicates for read-only workflow lists; writes use workflow services."""
from people import roles

from workflow.catalog import IMPORT_ONLY_STAGE_TYPES
from django.db.models import Case, When, Value, CharField, Exists, OuterRef, Q, F
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import OPPOSITE_VERIFIER, ROLE_GROUPS, STAGE_ROLE_MAP


def stage_role():
    kinds = {}
    for kind, role in STAGE_ROLE_MAP.items():
        kinds.setdefault(role, []).append(kind)
    return Case(
        *[When(stage_type__in=values, then=Value(role)) for role, values in kinds.items()],
        default=Value(''), output_field=CharField(),
    )


def current_stages():
    return S.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))


def _open():
    """Etap otwarty: niezakończony i bez daty końca."""
    return Q(is_completed=False, ended_at__isnull=True)


def _active(today):
    """Etap w toku: otwarty, wydany i rozpoczęty najpóźniej dziś."""
    return _open() & Q(is_released=True, started_at__lte=today)


def unfinished(query):
    return query.filter(_open())


def active_stages(query, today):
    return query.filter(_active(today))


def terminal(query):
    return query.filter(stage_type__in=('ready', 'withdrawn'))


def own_stages(user):
    assigned = A.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'),
                                assigned_to_id=user.pk, role=OuterRef('_work_role'))
    return S.objects.alias(_work_role=stage_role()).filter(
        Q(assignment__assigned_to_id=user.pk) | (Q(assignment__isnull=True, is_current=True) & Q(Exists(assigned)))
    ).exclude(stage_type__in=('author_editing', 'ready_for_editing', *IMPORT_ONLY_STAGE_TYPES))


def waiting_editor(query, today):
    return (Exists(unfinished(query).filter(stage_type__in=('first_verification', 'second_verification', 'author_editing')))
            & ~Exists(active_stages(query, today).filter(stage_type='editing')))


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
        | (~Exists(active_stages(matching, today)) & Exists(unfinished(matching)))
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


def _text_terms(user, today):
    """Terms of the user's work (_classify) that depend on the whole text, not on the user's own stages.

    Every term is a non-null EXISTS, evaluated per text in one query and combined by work_states().
    """
    stages = S.objects.current_cycle().filter(text_id=OuterRef('pk'))
    assigned = A.objects.current_cycle().filter(text_id=OuterRef('pk'), assigned_to_id=user.pk)
    return {
        'closed': Exists(terminal(stages)),
        'editor_assigned': Exists(assigned.filter(role=A.Role.EDITOR)),
        # Praca redaktora pozostaje w toku podczas poprawek autora. To klasyfikacja
        # tekstu, nie przypisanie redaktorowi etapu wykonywanego przez autora.
        'author_editing': Exists(unfinished(stages).filter(
            stage_type=S.StageType.AUTHOR_EDITING, is_released=True,
        )),
        'approved': editorial_approval(stages, today),
    }


def _own_stage_rows(user, today):
    """Własne etapy osoby (own_stages()) w bieżącym cyklu z warunkami „otwarty” i „w toku” liczonymi w wierszu.

    Wiersze: (pk, text_id, workflow_cycle, is_current, is_completed, rola pracy, otwarty, w toku).
    """
    return own_stages(user).filter(workflow_cycle=F('text__current_workflow_cycle')).annotate(
        work_role=F('_work_role'), stage_open=_open(), stage_active=_active(today),
    ).values_list('pk', 'text_id', 'workflow_cycle', 'is_current', 'is_completed', 'work_role',
                  'stage_open', 'stage_active')


def work_states(terms, reserved):
    """(active, waiting, completed) of one text from its _classify() terms and the reservation flag:
      active    = own_active and not closed
      waiting   = not own_active and not closed and (pending_own or reserved or editor waits for approval)
      completed = own_completed and not pending_own and not editor_remaining
                  and (closed or not own_active and not reserved)
    The states are mutually exclusive.
    """
    own_active, closed, pending_own = terms['own_active'], terms['closed'], terms['pending_own']
    editor_assigned, approved = terms['editor_assigned'], terms['approved']
    editor_remaining = (editor_assigned or terms['own_editor']) and not approved
    # The editor returns only for their own control, covered by pending_own
    # and own_active; the coordinator's control is not the editor's task.
    # An assigned editor waits until editorial approval (editor_remaining & editor_assigned).
    active = own_active and not closed
    waiting = not own_active and not closed and (pending_own or reserved or (editor_assigned and not approved))
    completed = (terms['own_completed'] and not pending_own and not editor_remaining
                 and (closed or (not own_active and not reserved)))
    return active, waiting, completed


def available_stages(user, access):
    from workflow.availability import can_claim_fourth_proofreading, first_proofreading_work
    stages = current_stages()
    checkpoints = S.objects.filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'),
                                    is_completed=True, is_skipped=False).exclude(repetition__canceled_at__isnull=False)
    assignments = A.objects.current_cycle().filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'))
    query = S.objects.current_cycle().exclude(text__anthology__status__in=("ready", "abandoned")).filter(
        is_released=True, is_completed=False, started_at__isnull=True, ended_at__isnull=True,
    ).alias(_work_role=stage_role())
    if not access['member']:
        return query.none()
    kinds = [kind for kind, role in STAGE_ROLE_MAP.items()
             if (kind != 'styling' or user.is_superuser)
             and (kind not in ('second_proofreading', 'fourth_proofreading') or can_claim_fourth_proofreading(user))
             and (access['coordinator'] or ROLE_GROUPS.get(role, roles.EDITOR).casefold() in access['roles'])]
    from workflow.catalog import EXTRACTS_STAGE_TYPES, extracts_text_q
    # Ekstrakty: wyłączone etapy (także zaległe) nie trafiają do „Teksty do wzięcia”.
    query = query.exclude(Q(extracts_text_q('text__')) & ~Q(stage_type__in=EXTRACTS_STAGE_TYPES))
    query = query.filter(stage_type__in=kinds).exclude(stage_type='editor_control', repetition__isnull=True).filter(
        ~Exists(terminal(stages)),
        ~Exists(assignments.filter(role=OuterRef('_work_role'), assigned_to__isnull=False)),
    )
    query = query.filter(~Q(stage_type__in=('second_proofreading', 'third_proofreading')) | ~Exists(first_proofreading_work(user).filter(text_id=OuterRef('text_id'))))
    opposite = Case(*[When(_work_role=role, then=Value(other)) for role, other in OPPOSITE_VERIFIER.items()],
                    default=Value(''), output_field=CharField())
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
        & ~Exists(unfinished(stages).filter(stage_type__in=('editing', 'author_editing')))))
    return query.annotate(reservation_only=(
        Q(stage_type='first_verification', repetition__isnull=True)
        & Exists(stages.filter(stage_type__in=('editing', 'author_editing'),
                               is_completed=False, ended_at__isnull=True))
    )).order_by('text__anthology__title', 'text__title', 'text_id', '-pk')


def user_text_ids(user):
    """Texts where the user holds any role or stage assignment (all cycles).

    Every work state in _classify requires such an assignment, so this
    narrows the classification to the user's own texts.
    """
    ids = set(A.objects.filter(assigned_to_id=user.pk).values_list('text_id', flat=True))
    ids.update(S.objects.filter(assignment__assigned_to_id=user.pk).values_list('text_id', flat=True))
    return ids


class _Work:
    """Work state of the user's texts (_classify)."""

    def __init__(self, active=(), waiting=(), editor_waiting=(), editor_active=(), reserved_rows=(),
                 active_own_stages=(), completed=()):
        self.active, self.waiting, self.editor_waiting = list(active), list(waiting), list(editor_waiting)
        self.completed = list(completed)
        # Active texts with the user as editor: only there author_editing is the user's dashboard work.
        self.editor_active = list(editor_active)
        self.reserved_rows = list(reserved_rows)  # (pk, text_id, role) of reserved_assignments()
        self.active_own_stages = list(active_own_stages)  # active own stages of active texts


def _classify(user, today):
    """Work state of the user's texts and their reservations, computed once.

    Fetched as values: the user's own stages once (_own_stage_rows), the text
    terms in one query (_text_terms) and reservations once at the top level –
    reserved_assignments() keeps the current cycle of the text, so a text is
    reserved exactly when one of its rows points to it. work_states() combines them.
    """
    from texts.models import Text
    candidates = user_text_ids(user)
    if not candidates:
        return _Work()
    own = {}
    for row in _own_stage_rows(user, today):
        own.setdefault((row[1], row[2]), []).append(row)
    terms = _text_terms(user, today)
    names = list(terms)
    texts = []
    for pk, cycle, *values in Text.objects.filter(pk__in=candidates).annotate(**terms).values_list(
        'pk', 'current_workflow_cycle', *names,
    ):
        fetched = {name: bool(value) for name, value in zip(names, values)}
        rows = own.get((pk, cycle), ())
        active_own = [stage_pk for stage_pk, _, _, current, _, _, _, active in rows if current and active]
        fetched.update(
            own_active=bool(active_own) or (fetched['editor_assigned'] and fetched['author_editing']),
            pending_own=any(current and is_open for _, _, _, current, _, _, is_open, _ in rows),
            own_editor=any(role == A.Role.EDITOR for _, _, _, _, _, role, _, _ in rows),
            own_completed=any(completed for _, _, _, _, completed, _, _, _ in rows),
        )
        texts.append((pk, fetched, active_own))
    # Rezerwacja zmienia tylko stan „oczekuje”, możliwy jedynie bez aktywnej pracy
    # i bez etapu końcowego; bez takich tekstów nie ma też listy rezerwacji.
    reserved_rows = []
    if any(not fetched['own_active'] and not fetched['closed'] for _, fetched, _ in texts):
        reserved_rows = list(reserved_assignments(user, today).values_list('pk', 'text_id', 'role'))
    reserved_texts = {text_id for _, text_id, _ in reserved_rows}
    work = _Work(reserved_rows=reserved_rows)
    for pk, fetched, active_own in texts:
        is_active, is_waiting, is_completed = work_states(fetched, pk in reserved_texts)
        if is_completed:
            work.completed.append(pk)
        if is_active:
            work.active.append(pk)
            work.active_own_stages.extend(active_own)
            if fetched['editor_assigned']:
                work.editor_active.append(pk)
        if is_waiting:
            work.waiting.append(pk)
            if fetched['editor_assigned']:
                work.editor_waiting.append(pk)
    return work


def dashboard_querysets(user, today):
    # Klasyfikacja tekstów i rezerwacje są liczone raz, tylko dla tekstów użytkownika;
    # dalsze zapytania dostają gotowe listy identyfikatorów zamiast podzapytań.
    # Teksty w toku i oczekujące nie mają etapu końcowego w bieżącym cyklu (warunek
    # „closed” w work_states), więc osobne sprawdzanie go tutaj byłoby zbędne.
    work = _classify(user, today)
    stages = current_stages()
    editor = A.objects.current_cycle().filter(text_id=OuterRef('text_id'),
        workflow_cycle=OuterRef('workflow_cycle'), role=A.Role.EDITOR, assigned_to_id=user.pk)
    editorial_waiting_ids = []
    if work.editor_waiting:
        from workflow.state import current_stage_subquery
        representative = current_stage_subquery(stages, 'pk')
        editorial_waiting_ids = list(S.objects.current_cycle().filter(
            text_id__in=work.editor_waiting, is_released=True, pk=representative,
        ).values_list('pk', flat=True))
    # Praca na pulpicie: aktywne własne etapy (z klasyfikacji) oraz poprawki autora
    # w tekstach, w których osoba jest redaktorem (own_stages() ich nie obejmuje).
    active_ids = list(work.active_own_stages)
    if work.editor_active:
        active_ids += active_stages(S.objects.filter(stage_type=S.StageType.AUTHOR_EDITING).filter(Exists(editor)),
                                    today).filter(text_id__in=work.editor_active).filter(
            workflow_cycle=F('text__current_workflow_cycle'), is_current=True).values_list('pk', flat=True)
    active = S.objects.filter(pk__in=[*active_ids, *editorial_waiting_ids]).annotate(
        editor_waiting=Q(pk__in=editorial_waiting_ids)
            | (Q(stage_type=S.StageType.AUTHOR_EDITING) & Q(Exists(editor))),
    ).order_by(F('started_at').desc(nulls_last=True), '-pk')
    waiting, editor_waiting = set(work.waiting), set(work.editor_waiting)
    reserved_ids = [pk for pk, text_id, role in work.reserved_rows if text_id in waiting
                    and not (role == A.Role.EDITOR and text_id in editor_waiting)]
    reserved = (A.objects.filter(pk__in=reserved_ids).order_by(F('assigned_at').desc(nulls_last=True), '-pk')
                if reserved_ids else A.objects.none())
    return active, reserved


def classify_work(user, today):
    """Work states of all the user's texts: {'active', 'waiting', 'completed'} → sets of text IDs."""
    work = _classify(user, today)
    return {'active': set(work.active), 'waiting': set(work.waiting), 'completed': set(work.completed)}


def work_text_ids(queryset, user, today):
    """Internal work states shared by row badges, profile and dashboard, limited to `queryset`."""
    ids = set(queryset.values_list('pk', flat=True))
    return {name: found & ids for name, found in classify_work(user, today).items()}
