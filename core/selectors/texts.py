from core.filtering import facet_queryset
from datetime import date

from django.db.models import (
    Case,
    F,
    IntegerField,
    Q,
    OuterRef,
    Prefetch,
    Subquery,
    Value,
    When,
)
from django.shortcuts import get_object_or_404
from django.utils import timezone

from authors.models import Author
from core.permissions import (
    can_view_author_data,
    has_role,
    is_coordinator,
    require_team_member,
)
from texts.models import Anthology, Review, ReviewAssignment, Text, TextNote
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import ROLE_GROUPS, STAGE_ROLES


StageType = WorkflowStage.StageType
Role = WorkflowRoleAssignment.Role

STAGE_ROLE_MAP = {
    **STAGE_ROLES,
    StageType.READY_FOR_EDITING: Role.EDITOR,
    StageType.EDITING: Role.EDITOR,
    StageType.AUTHOR_EDITING: Role.EDITOR,
    StageType.EDITOR_CONTROL: Role.EDITOR,
}

TERMINAL_STAGES = frozenset({StageType.READY, StageType.WITHDRAWN})
VERIFICATION_STAGES = frozenset(
    {StageType.FIRST_VERIFICATION, StageType.SECOND_VERIFICATION}
)
EDITOR_PHASE_STAGES = frozenset(
    {
        StageType.READY_FOR_EDITING,
        StageType.EDITING,
        StageType.AUTHOR_EDITING,
        *VERIFICATION_STAGES,
    }
)
MAX_DATABASE_ID = 9_223_372_036_854_775_807

TEXT_SORTS = {
    "title": ("title", "pk"),
    "-title": ("-title", "-pk"),
    "anthology": ("anthology__title", "title", "pk"),
    "-anthology": ("-anthology__title", "title", "pk"),
    "length": ("length", "title", "pk"),
    "-length": ("-length", "title", "pk"),
    "status": ("current_stage_type", "title", "pk"),
    "-status": ("-current_stage_type", "title", "pk"),
}
WORKFLOW_SORTS = {
    "title": ("text__title", "pk"),
    "-title": ("-text__title", "-pk"),
    "anthology": ("text__anthology__title", "text__title", "-pk"),
    "stage": ("stage_type", "text__title", "-pk"),
    "started_at": (F("started_at").asc(nulls_last=True), "pk"),
    "-started_at": (F("started_at").desc(nulls_last=True), "-pk"),
}


class _Record(dict):
    """Jawna projekcja danych, bez dostępu do relacji ORM."""

    def __str__(self):
        return (
            self.get("display_name")
            or self.get("title")
            or self.get("name")
            or ""
        )


class _ProjectedRows:
    """Pozwala stronicować QuerySet przed przygotowaniem wierszy."""

    def __init__(self, queryset, projector):
        self.queryset = queryset
        self.projector = projector

    def count(self):
        return self.queryset.count()

    def __len__(self):
        return self.count()

    def __iter__(self):
        for item in self.queryset:
            yield self.projector(item)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [self.projector(item) for item in self.queryset[key]]
        return self.projector(self.queryset[key])


def _positive_id(value):
    value = (value or "").strip()
    if (
        not value
        or len(value) > 19
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    result = int(value)
    return result if 1 <= result <= MAX_DATABASE_ID else None


def _person_data(person):
    if person is None:
        return None
    return _Record(
        pk=person.pk,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=str(person),
        is_active=person.is_active,
    )


def _user_data(user):
    if user is None:
        return None
    return _Record(
        pk=user.pk,
        username="",
        get_full_name=user.get_full_name(),
        display_name=user.get_full_name() or "Nieuzupełnione dane",
        person_profile=_person_data(getattr(user, "person_profile", None)),
    )


def _author_data(author):
    return _Record(
        pk=author.pk,
        first_name=author.first_name,
        last_name=author.last_name,
        pseudonym=author.pseudonym,
        email=author.email,
        display_name=str(author),
    )


def _anthology_data(anthology):
    if anthology is None:
        return None
    return _Record(pk=anthology.pk, title=anthology.title)


def _stage_data(stage, text_data=None):
    if stage is None:
        return None
    result = _Record(
        pk=stage.pk,
        text_id=stage.text_id,
        workflow_cycle=stage.workflow_cycle,
        stage_type=stage.stage_type,
        get_stage_type_display=stage.get_stage_type_display(),
        iteration=stage.iteration,
        started_at=stage.started_at,
        ended_at=stage.ended_at,
        is_completed=stage.is_completed,
    )
    if text_data is not None:
        result["text"] = text_data
    return result


def _assignment_data(assignment, text_data=None):
    if assignment is None:
        return None
    result = _Record(
        pk=assignment.pk,
        text_id=assignment.text_id,
        workflow_cycle=assignment.workflow_cycle,
        role=assignment.role,
        get_role_display=assignment.get_role_display(),
        assigned_to_id=assignment.assigned_to_id,
        assigned_to=_user_data(assignment.assigned_to),
        assigned_at=assignment.assigned_at,
        notes=assignment.notes,
    )
    if text_data is not None:
        result["text"] = text_data
    return result


def _text_data(text, include_authors):
    authors = (
        [_author_data(author) for author in text.selector_authors]
        if include_authors
        else []
    )
    return _Record(
        pk=text.pk,
        title=text.title,
        length=text.length,
        anthology_id=text.anthology_id,
        anthology=_anthology_data(text.anthology),
        current_workflow_cycle=text.current_workflow_cycle,
        content_warnings=text.content_warnings,
        coordinator_note=text.coordinator_note,
        authors={"all": authors},
        authors_display=", ".join(str(author) for author in authors),
        author_emails=", ".join(
            author["email"] for author in authors if author["email"]
        ),
    )


def _prepared_texts(queryset, include_authors):
    stages = WorkflowStage.objects.filter(
        workflow_cycle=F("text__current_workflow_cycle"),
    ).order_by("-pk")
    assignments = (
        WorkflowRoleAssignment.objects.filter(
            workflow_cycle=F("text__current_workflow_cycle"),
        )
        .select_related("assigned_to", "assigned_to__person_profile")
        .order_by("role", "pk")
    )
    queryset = queryset.select_related("anthology").prefetch_related(
        Prefetch(
            "workflow_stages",
            queryset=stages,
            to_attr="selector_stages",
        ),
        Prefetch(
            "workflow_role_assignments",
            queryset=assignments,
            to_attr="selector_assignments",
        ),
    )
    if include_authors:
        queryset = queryset.prefetch_related(
            Prefetch(
                "authors",
                queryset=Author.objects.order_by("last_name", "first_name", "pk"),
                to_attr="selector_authors",
            )
        )
    return queryset


def _is_open(stage):
    return not stage.is_completed and stage.ended_at is None


def _is_active(stage, today):
    return (
        _is_open(stage)
        and stage.started_at is not None
        and stage.started_at <= today
    )


def _terminal(stages):
    return any(stage.stage_type in TERMINAL_STAGES for stage in stages)


def _current_stage(stages):
    open_stages = [stage for stage in stages if _is_open(stage)]
    if not open_stages:
        return None
    return min(
        open_stages,
        key=lambda stage: (
            0 if stage.stage_type == StageType.WITHDRAWN else
            1 if stage.stage_type == StageType.AUTHOR_EDITING else 2,
            -(stage.started_at or date.min).toordinal(),
            -stage.pk,
        ),
    )


def _annotated_texts():
    current = (
        WorkflowStage.objects.filter(
            text_id=OuterRef("pk"),
            workflow_cycle=OuterRef("current_workflow_cycle"),
            is_completed=False,
            ended_at__isnull=True,
        )
        .annotate(
            priority=Case(
                When(stage_type=StageType.WITHDRAWN, then=Value(0)),
                When(stage_type=StageType.AUTHOR_EDITING, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("priority", F("started_at").desc(nulls_last=True), "-pk")
        .values("stage_type")[:1]
    )
    return Text.objects.annotate(current_stage_type=Subquery(current))


def _text_row(text, include_authors):
    result = _text_data(text, include_authors)
    current = _current_stage(text.selector_stages)
    current_data = _stage_data(current)
    result.update(
        current_stage_type=current.stage_type if current else None,
        current_status=current_data,
        current_stages=[
            _stage_data(stage)
            for stage in text.selector_stages
            if _is_open(stage)
        ],
        current_status_started_at=current.started_at if current else None,
        current_status_ended_at=current.ended_at if current else None,
    )
    return result


def text_list_context(*, user, params):
    require_team_member(user)
    include_authors = can_view_author_data(user)
    anthology_id = _positive_id(params.get("anthology"))
    author_id = _positive_id(params.get("author")) if include_authors else None
    valid_statuses = set(StageType.values) | {"none"}
    requested = params.getlist("status") if hasattr(params, "getlist") else [params.get("status", "")]
    statuses = list(dict.fromkeys(v for v in requested if v in valid_statuses))
    status = statuses[-1] if statuses else ""
    query = params.get("q", "").strip()[:500]

    hide_ready = params.get("hide_ready", "1").strip() != "0"
    if StageType.READY in statuses:
        hide_ready = False

    def filtered(exclude=None):
        result = _annotated_texts()
        if anthology_id is not None and exclude != 'anthology':
            result = result.filter(anthology_id=anthology_id)
        if author_id is not None and exclude != 'author':
            result = result.filter(authors__pk=author_id)
        if statuses and exclude != 'status':
            condition = Q(current_stage_type__in=[v for v in statuses if v != 'none'])
            if 'none' in statuses:
                condition |= Q(current_stage_type__isnull=True)
            result = result.filter(condition)
        for term in query.split():
            condition = Q(title__plcontains=term) | Q(anthology__title__plcontains=term)
            if include_authors:
                condition |= Q(authors__first_name__plcontains=term) | Q(authors__last_name__plcontains=term) | Q(authors__pseudonym__plcontains=term)
            result = result.filter(condition)
        if hide_ready:
            result = result.exclude(current_stage_type=StageType.READY)
        return result.distinct()

    queryset = filtered()
    anthology_options = Anthology.objects.filter(
        Q(pk__in=filtered('anthology').values('anthology_id')) | Q(pk=anthology_id))
    author_options = Author.objects.filter(
        Q(pk__in=filtered('author').values('authors__pk')) | Q(pk=author_id)) if include_authors else Author.objects.none()
    available_statuses = set(filtered('status').values_list('current_stage_type', flat=True))

    sort = params.get("sort", "anthology")
    if sort not in TEXT_SORTS:
        sort = "anthology"

    queryset = _prepared_texts(
        queryset.order_by(*TEXT_SORTS[sort]),
        include_authors,
    )
    return {
        "texts": _ProjectedRows(
            queryset,
            lambda text: _text_row(text, include_authors),
        ),
        "anthologies": list(
            anthology_options.order_by("title", "pk").values("pk", "title")
        ),
        "filter_authors": (
            [_author_data(author) for author in author_options]
            if include_authors
            else []
        ),
        "status_choices": [(v, label) for v, label in StageType.choices if v in available_statuses or v in statuses],
        "show_no_status_filter": None in available_statuses or "none" in statuses,
        "selected_anthology_id": str(anthology_id) if anthology_id else "",
        "selected_author_id": str(author_id) if author_id else "",
        "selected_status": status,
        "selected_statuses": statuses,
        "query": query,
        "hide_ready": hide_ready,
        "sort": sort,
    }


def _user_texts(user, include_authors):
    return _prepared_texts(
        Text.objects.filter(
            workflow_role_assignments__assigned_to_id=user.pk,
            workflow_role_assignments__workflow_cycle=F("current_workflow_cycle"),
        ).distinct().order_by("anthology__title", "title", "pk"),
        include_authors,
    )


def _own_work(text, user, today):
    assignments = [
        item for item in text.selector_assignments
        if item.assigned_to_id == user.pk
    ]
    roles = {item.role for item in assignments}
    stages = text.selector_stages
    if _terminal(stages):
        return assignments, [], [], False

    own_stages = [
        stage for stage in stages
        if STAGE_ROLE_MAP.get(stage.stage_type) in roles
        and stage.stage_type not in {
            StageType.AUTHOR_EDITING, StageType.READY_FOR_EDITING,
        }
    ]
    active = [stage for stage in own_stages if _is_active(stage, today)]
    reserved = []
    for assignment in assignments:
        matching = [
            stage for stage in own_stages
            if STAGE_ROLE_MAP.get(stage.stage_type) == assignment.role
        ]
        if not matching or (
            not any(_is_active(stage, today) for stage in matching)
            and any(_is_open(stage) for stage in matching)
        ):
            reserved.append(assignment)

    waiting = (
        Role.EDITOR in roles
        and not any(
            stage.stage_type == StageType.EDITING and _is_active(stage, today)
            for stage in stages
        )
        and any(
            stage.stage_type in VERIFICATION_STAGES | {StageType.AUTHOR_EDITING}
            and _is_open(stage)
            for stage in stages
        )
    )
    if waiting:
        reserved = [item for item in reserved if item.role != Role.EDITOR]
    return assignments, active, reserved, waiting


def my_texts_context(*, user, selected_view="active"):
    require_team_member(user)
    if selected_view not in {"active", "waiting", "completed", "all"}:
        selected_view = "active"
    include_authors = can_view_author_data(user)
    today = timezone.localdate()
    rows = []

    for text in _user_texts(user, include_authors):
        assignments, active, reserved, waiting = _own_work(text, user, today)
        roles = {item.role for item in assignments}
        own_stages = [
            stage for stage in text.selector_stages
            if STAGE_ROLE_MAP.get(stage.stage_type) in roles
            and stage.stage_type not in {
                StageType.AUTHOR_EDITING, StageType.READY_FOR_EDITING,
            }
        ]
        completed = (
            bool(own_stages)
            and all(stage.is_completed for stage in own_stages)
            and not active and not reserved and not waiting
        )
        if selected_view == "active" and not active:
            continue
        if selected_view == "waiting" and not (waiting or reserved):
            continue
        if selected_view == "completed" and not completed:
            continue

        row = _text_row(text, include_authors)
        row.update(
            user_assignments=[_assignment_data(item) for item in assignments],
            visible_assignments=[_assignment_data(item) for item in assignments],
            current_cycle_stage_history=[
                _stage_data(stage) for stage in text.selector_stages
            ],
            has_active_work=bool(active),
            has_reserved_work=bool(reserved),
            has_completed_work=completed,
            is_waiting_for_other_role=waiting,
        )
        rows.append(row)

    return {"texts": rows, "selected_view": selected_view}


def user_workflow_summary(user, *, today=None):
    require_team_member(user)
    today = today or timezone.localdate()
    include_authors = can_view_author_data(user)
    active_rows = []
    reserved_rows = []

    for text in _user_texts(user, include_authors):
        _, active, reserved, _ = _own_work(text, user, today)
        text_data = _text_data(text, include_authors)
        active_rows.extend(_stage_data(stage, text_data) for stage in active)
        reserved_rows.extend(
            _assignment_data(assignment, text_data)
            for assignment in reserved
        )

    active_rows.sort(key=lambda row: (row["started_at"], row["pk"]))
    return {
        "active_stages": active_rows,
        "reserved_assignments": reserved_rows,
        "active_stage_count": len(active_rows),
        "reserved_assignment_count": len(reserved_rows),
    }


def available_stages_for_user(*, user):
    require_team_member(user)
    include_authors = can_view_author_data(user)
    coordinator = is_coordinator(user)
    allowed_roles = {
        role for role, group in ROLE_GROUPS.items()
        if coordinator or has_role(user, group)
    }
    if not user.is_superuser:
        allowed_roles.discard(Role.STYLING)
    if coordinator or has_role(user, "Redaktor"):
        allowed_roles.add(Role.EDITOR)

    texts = _prepared_texts(
        Text.objects.filter(
            workflow_stages__workflow_cycle=F("current_workflow_cycle"),
            workflow_stages__is_completed=False,
            workflow_stages__started_at__isnull=True,
            workflow_stages__ended_at__isnull=True,
        ).distinct().order_by("anthology__title", "title", "pk"),
        include_authors,
    )
    rows = []

    for text in texts:
        stages = text.selector_stages
        if _terminal(stages):
            continue

        occupied = {
            assignment.role: assignment.assigned_to_id
            for assignment in text.selector_assignments
            if assignment.assigned_to_id is not None
        }
        for stage in stages:
            role = STAGE_ROLE_MAP.get(stage.stage_type)
            if (
                not _is_open(stage)
                or stage.started_at is not None
                or role not in allowed_roles
                or role in occupied
                or stage.stage_type in {
                    StageType.EDITING,
                    StageType.AUTHOR_EDITING,
                    StageType.EDITOR_CONTROL,
                }
            ):
                continue

            opposite = {
                Role.VERIFIER_1: Role.VERIFIER_2,
                Role.VERIFIER_2: Role.VERIFIER_1,
            }.get(role)
            if opposite and occupied.get(opposite) == user.pk:
                continue

            if stage.stage_type == StageType.SECOND_VERIFICATION:
                if not any(
                    item.stage_type == StageType.FIRST_VERIFICATION
                    and item.is_completed for item in stages
                ):
                    continue
                if any(
                    item.stage_type in {StageType.EDITING, StageType.AUTHOR_EDITING}
                    and _is_open(item) for item in stages
                ):
                    continue

            row = _stage_data(stage, _text_data(text, include_authors))
            row.update(
                required_group="Superuser" if role == Role.STYLING else ROLE_GROUPS.get(role, "Redaktor"),
                available_role=role,
            )
            rows.append(row)

    return rows


def workflow_list_context(*, user, params):
    require_team_member(user)
    include_authors = can_view_author_data(user)
    requested_anthologies = (
        params.getlist("anthology") if hasattr(params, "getlist")
        else [params.get("anthology", "")]
    )
    anthology_ids = list(dict.fromkeys(
        identifier for value in requested_anthologies
        if (identifier := _positive_id(value)) is not None
    ))
    requested_stages = (
        params.getlist("stage") if hasattr(params, "getlist")
        else [params.get("stage", "")]
    )
    selected_stages = list(dict.fromkeys(
        value.strip() for value in requested_stages
        if value.strip() in StageType.values
    ))
    query = params.get("q", "").strip()

    # Wyszukiwanie po autorze nie jest wykonywane dla koordynatora.
    from django.db.models import Q

    stages = WorkflowStage.objects.filter(
        workflow_cycle=F("text__current_workflow_cycle"),
    )
    for term in query.split():
        condition = (
            Q(text__title__plcontains=term)
            | Q(text__anthology__title__plcontains=term)
        )
        if include_authors:
            condition |= (
                Q(text__authors__first_name__plcontains=term)
                | Q(text__authors__last_name__plcontains=term)
                | Q(text__authors__pseudonym__plcontains=term)
                | Q(text__authors__email__plcontains=term)
            )
        stages = stages.filter(condition)

    stages, facets = facet_queryset(stages, {
        'stage': ('stage_type', selected_stages),
        'anthology': ('text__anthology_id', anthology_ids),
    })
    sort = params.get("sort", "anthology")
    if sort not in WORKFLOW_SORTS:
        sort = "anthology"

    stages = (
        stages.distinct()
        .order_by(*WORKFLOW_SORTS[sort])
        .prefetch_related(
            Prefetch(
                "text",
                queryset=_prepared_texts(Text.objects.all(), include_authors),
            )
        )
    )

    selected_roles = {STAGE_ROLE_MAP.get(value) for value in selected_stages}
    role_columns = [item for item in Role.choices if not selected_stages or item[0] in selected_roles]

    def project(stage):
        text = stage.text
        assignments = {
            item.role: item for item in text.selector_assignments
        }
        row = _stage_data(stage, _text_data(text, include_authors))
        row["role_cells"] = [
            {
                "role": role,
                "label": label,
                "user": (
                    _user_data(assignments[role].assigned_to)
                    if role in assignments else None
                ),
            }
            for role, label in role_columns
        ]
        return row

    return {
        "stages": _ProjectedRows(stages, project),
        "anthologies": list(
            Anthology.objects.filter(pk__in=facets["anthology"]).order_by("title", "pk").values("pk", "title")
        ),
        "selected_stages": selected_stages,
        "stage_choices": [(v, label) for v, label in StageType.choices if v in facets["stage"]],
        "role_columns": role_columns,
        "selected_anthology_ids": anthology_ids,
        "query": query,
        "sort": sort,
    }


from core.selectors.history import merge_historical_team_members


def text_detail_context(*, user, text):
    require_team_member(user)
    include_authors = can_view_author_data(user)
    coordinator = is_coordinator(user)
    today = timezone.localdate()

    text = get_object_or_404(
        _prepared_texts(Text.objects.all(), include_authors),
        pk=text.pk,
    )
    text_data = _text_data(text, include_authors)
    stages = text.selector_stages
    assignments = {
        item.role: item for item in text.selector_assignments
    }
    own = [
        item for item in text.selector_assignments
        if item.assigned_to_id == user.pk
    ]
    terminal = _terminal(stages)
    is_withdrawn = any(
        stage.stage_type == StageType.WITHDRAWN for stage in stages
    )
    is_ready = any(stage.stage_type == StageType.READY for stage in stages)

    def owned(role):
        assignment = assignments.get(role)
        return bool(assignment and assignment.assigned_to_id == user.pk)

    def active(kind):
        return next(
            (
                stage for stage in stages
                if stage.stage_type == kind and _is_active(stage, today)
            ),
            None,
        )

    def pending(kind):
        return next(
            (
                stage for stage in stages
                if stage.stage_type == kind
                and _is_open(stage) and stage.started_at is None
            ),
            None,
        )

    def completed(kind):
        return any(
            stage.stage_type == kind and stage.is_completed
            for stage in stages
        )

    editor = assignments.get(Role.EDITOR)
    first_verifier = assignments.get(Role.VERIFIER_1)
    editing = active(StageType.EDITING)
    author_editing = active(StageType.AUTHOR_EDITING)
    pending_editing = pending(StageType.EDITING)
    pending_first = pending(StageType.FIRST_VERIFICATION)
    verification = next(
        (
            stage for stage in stages
            if stage.stage_type in VERIFICATION_STAGES
            and _is_active(stage, today)
        ),
        None,
    )
    verification_open = any(
        stage.stage_type in VERIFICATION_STAGES and _is_open(stage)
        for stage in stages
    )
    later_phase = any(
        stage.stage_type not in EDITOR_PHASE_STAGES
        for stage in stages
    )
    can_control = (
        not terminal and not later_phase
        and (coordinator or owned(Role.EDITOR))
    )
    first_done = completed(StageType.FIRST_VERIFICATION)
    second_done = completed(StageType.SECOND_VERIFICATION)

    can_start_first = bool(
        not terminal and pending_first
        and first_verifier and first_verifier.assigned_to_id
        and not any(
            stage.stage_type in {StageType.EDITING, StageType.AUTHOR_EDITING}
            and _is_open(stage) for stage in stages
        )
        and (coordinator or owned(Role.VERIFIER_1))
    )
    can_resume = bool(
        can_control and not editing and not verification_open
        and (author_editing or pending_editing)
    )

    from core.selectors.people import user_leave_information
    leave_cache = {}
    stage_rows = []
    for stage in stages:
        role = STAGE_ROLE_MAP.get(stage.stage_type)
        assignment = assignments.get(role)
        assigned_user = assignment.assigned_to if assignment else None
        if assigned_user and assigned_user.pk not in leave_cache:
            leave_cache[assigned_user.pk] = user_leave_information(assigned_user)
        row = _stage_data(stage, text_data)

        may_act = coordinator or owned(role)
        if stage.stage_type == StageType.EDITOR_CONTROL:
            may_act = owned(Role.EDITOR)

        can_start = bool(
            not terminal and role and assignment
            and assignment.assigned_to_id
            and may_act and _is_open(stage)
            and stage.started_at is None
            and stage.stage_type not in TERMINAL_STAGES | {
                StageType.AUTHOR_EDITING,
            }
        )
        if stage.stage_type == StageType.FIRST_VERIFICATION:
            can_start = can_start_first
        elif stage.stage_type == StageType.EDITING:
            can_start = can_start and can_resume

        if stage.stage_type == StageType.STYLING:
            can_start = can_start and user.is_superuser and bool(assigned_user and assigned_user.is_superuser)
        row.update(
            can_claim_styling=bool(
                user.is_superuser and not terminal and stage.stage_type == StageType.STYLING
                and _is_open(stage) and stage.started_at is None and not assigned_user
            ),
            assigned_role=role,
            assigned_user=_user_data(assigned_user),
            leave_information=leave_cache.get(assigned_user.pk) if assigned_user else None,
            assigned_person=_person_data(
                getattr(assigned_user, "person_profile", None)
                if assigned_user else None
            ),
            can_start=can_start,
            can_complete=bool(
                not terminal and role and may_act
                and _is_active(stage, today)
                and stage.stage_type not in TERMINAL_STAGES | {
                    StageType.READY_FOR_EDITING,
                    StageType.EDITING,
                    StageType.AUTHOR_EDITING,
                }
            ),
        )
        stage_rows.append(row)

    row_by_id = {row["pk"]: row for row in stage_rows}

    def stage_row(stage):
        return row_by_id.get(stage.pk) if stage else None

    current = _current_stage(stages)
    previous = max(
        (stage for stage in stages if stage.is_completed),
        key=lambda stage: (stage.ended_at or date.min, stage.pk),
        default=None,
    )
    visible = [
        stage_row(stage) for stage in (current, previous) if stage is not None
    ]
    visible_ids = {row["pk"] for row in visible}

    archived = []
    if coordinator:
        historical_stages = WorkflowStage.objects.filter(
            text_id=text.pk,
        ).exclude(
            workflow_cycle=text.current_workflow_cycle,
        ).order_by("-workflow_cycle", "-pk")
        archived = [row for row in stage_rows if row["pk"] not in visible_ids]
        for stage in historical_stages:
            row = _stage_data(stage, text_data)
            row.update(can_start=False, can_complete=False)
            archived.append(row)

    notes = [
        {
            "pk": note.pk,
            "author": _user_data(note.author),
            "content": note.content,
            "is_important": note.is_important,
            "created_at": note.created_at,
        }
        for note in TextNote.objects.filter(text_id=text.pk)
        .select_related("author", "author__person_profile")
        .order_by("-created_at", "-pk")
    ]

    source_query = Review.objects.filter(copied_text_id=text.pk)
    if not include_authors:
        source_query = source_query.filter(old_reviews=False)
    source = source_query.first()
    source_data = None
    opinions = []

    if source is not None:
        source_data = _Record(
            pk=source.pk,
            title=source.title,
            genre=source.genre,
            status=source.status,
            get_status_display=source.get_status_display(),
            content_warnings=source.content_warnings,
            old_reviews=source.old_reviews,
        )
        if include_authors:
            source_data.update(
                author_first_name=source.author_first_name,
                author_last_name=source.author_last_name,
                email=source.email,
                phone_number=source.phone_number,
            )
        for assignment in (
            ReviewAssignment.objects.filter(review_id=source.pk)
            .select_related("user", "user__person_profile")
            .order_by("position", "pk")
        ):
            opinions.append(
                {
                    "slot": assignment.position,
                    "position": assignment.position,
                    "user": _user_data(assignment.user),
                    "reviewer_name": (
                        assignment.user.get_full_name()
                        or "Nieuzupełnione dane"
                        if assignment.user else "Usunięte konto"
                    ),
                    "opinion_value": assignment.opinion,
                    "opinion": assignment.get_opinion_display(),
                    "opinion_display": assignment.get_opinion_display(),
                    "notes": assignment.notes,
                    "status_changed_at": assignment.opinion_changed_at,
                }
            )

    return {
        "text": text_data,
        "user_assignments": [_assignment_data(item) for item in own],
        "stages": stage_rows,
        "visible_stages": visible,
        "archived_stages": archived,
        "can_view_stage_history": coordinator,
        "team_members": merge_historical_team_members(text, [
            {
                "role": role,
                "label": label,
                "assignment": _assignment_data(assignments.get(role)),
                "user": (
                    _user_data(assignments[role].assigned_to)
                    if role in assignments else None
                ),
                "is_assigned": bool(
                    role in assignments and assignments[role].assigned_to_id
                ),
            }
            for role, label in Role.choices
        ]),
        "is_assigned": bool(own),
        "is_read_only": not coordinator and not own,
        "can_add_note": coordinator or bool(own),
        "can_edit_coordinator_note": coordinator,
        "show_coordinator_note": bool(text.coordinator_note.strip()),
        "text_notes": notes,
        "source_review": source_data,
        "source_review_opinions": opinions,
        "is_withdrawn": is_withdrawn,
        "is_ready": is_ready,
        "can_manage_workflow": coordinator,
        "can_control_editing": can_control,
        "editor_assignment": _assignment_data(editor),
        "first_verifier_assignment": _assignment_data(first_verifier),
        "active_editing": stage_row(editing),
        "active_author_editing": stage_row(author_editing),
        "active_verification": stage_row(verification),
        "pending_editing": stage_row(pending_editing),
        "pending_first_verification": stage_row(pending_first),
        "first_verification_completed": first_done,
        "second_verification_completed": second_done,
        "can_send_to_first_verification": bool(
            can_control and editing and pending_first
            and first_verifier and first_verifier.assigned_to_id
            and not first_done
        ),
        "can_start_first_verification": can_start_first,
        "can_resume_editing": can_resume,
        "can_send_to_author": bool(
            can_control and editing and first_done and not verification_open
        ),
        "can_send_to_second_verification": bool(
            can_control and editing and first_done
            and not any(
                stage.stage_type == StageType.SECOND_VERIFICATION
                for stage in stages
            )
        ),
        "can_finish_editing": bool(
            can_control and editing and second_done and not verification_open
        ),
        "all_authors": (
            [_author_data(author) for author in Author.objects.all()]
            if include_authors else []
        ),
        "selected_author_ids": (
            {author.pk for author in text.selector_authors}
            if include_authors else set()
        ),
        "active_workflow_stage": next(
            (
                row_by_id[stage.pk] for stage in stages
                if not terminal and _is_active(stage, today)
            ),
            None,
        ),
        "active_user_stage": next(
            (row for row in stage_rows if row["can_complete"]),
            None,
        ),
    }
