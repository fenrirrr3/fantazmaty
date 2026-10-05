from workflow.catalog import active_stage_choices, active_role_choices, workflow_role_choices, IMPORT_ONLY_ROLES
from workflow.labels import assignment_label, execution_label, stage_label
from core.translation_scope import ordinary
from core.filtering import facet_queryset
from datetime import date

from django.db.models import (
    Case,
    Exists,
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
from texts.models import Anthology, Review, ReviewAssignment, Text, TextNote, ForeignAuthor, Translator
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
    "recent": ("_semantic_order", F("_assigned_time").desc(nulls_last=True), F("started_at").desc(nulls_last=True), "-pk"),
    "-anthology": ("-text__anthology__title", "-pk"),
    "-stage": ("-stage_type", "-pk"),
    "ended_at": (F("ended_at").asc(nulls_last=True), "pk"),
    "-ended_at": (F("ended_at").desc(nulls_last=True), "-pk"),
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
    return _Record(pk=anthology.pk, title=anthology.title, status=anthology.status, is_translated=anthology.is_translated)


def _stage_data(stage, text_data=None):
    if stage is None:
        return None
    result = _Record(
        pk=stage.pk,
        text_id=stage.text_id,
        workflow_cycle=stage.workflow_cycle,
        queue_position=stage.queue_position,
        imported_completed=stage.imported_completed,
        send_to_proofreading=stage.send_to_proofreading,
        is_skipped=stage.is_skipped,
        execution_number=stage.execution_number,
        repetition_id=stage.repetition_id,
        is_released=stage.is_released,
        stage_type=stage.stage_type,
        get_stage_type_display=stage_label(stage) + (" – powrót do redaktora" if stage.repetition_id and stage.stage_type == StageType.EDITING and stage.queue_position > 0 else ""),
        iteration=stage.iteration,
        started_at=stage.started_at,
        ended_at=stage.ended_at,
        is_completed=stage.is_completed,
        editor_waiting=getattr(stage, 'editor_waiting', False),
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
        get_role_display=assignment_label(assignment),
        assigned_to_id=assignment.assigned_to_id,
        assigned_to=_user_data(assignment.assigned_to),
        assigned_at=assignment.assigned_at,
        notes=assignment.notes,
    )
    if text_data is not None:
        result["text"] = text_data
    return result


def _text_data(text, include_authors):
    translated = bool(text.anthology_id and text.anthology.is_translated)
    record = getattr(text, "translation", None) if translated else None
    source_authors = getattr(text, "selector_authors", None)
    if translated:
        source_authors = record.foreign_authors.all() if record else []
    elif source_authors is None:
        source_authors = text.authors.all()
    authors = [
        _author_data(author) if include_authors else _Record(
            pk=author.pk, first_name=author.first_name, last_name=author.last_name,
            display_name=str(author),
        )
        for author in source_authors
    ]
    translated = bool(text.anthology_id and text.anthology.is_translated)
    record = getattr(text, "translation", None) if translated else None
    translators = list(record.translators.all()) if record else []
    return _Record(
        is_translation=translated,
        translators=[_author_data(a) if include_authors else _Record(pk=a.pk, first_name=a.first_name, last_name=a.last_name) for a in translators],
        translators_display=", ".join(str(a) for a in translators),
        pk=text.pk,
        title=text.title,
        length=text.length,
        anthology_id=text.anthology_id,
        anthology=_anthology_data(text.anthology),
        current_workflow_cycle=text.current_workflow_cycle,
        content_warnings=text.content_warnings,
        genre=text.genre,
        coordinator_note=text.coordinator_note,
        coordinator_note_updated_at=text.coordinator_note_updated_at,
        authors={"all": authors},
        authors_display=", ".join(str(author) for author in authors),
        author_emails=", ".join(
            author["email"] for author in authors if author.get("email")
        ),
    )


def _prepared_texts(queryset, include_authors):
    from workflow.read_queries import completed_verification_history

    stages = WorkflowStage.objects.current_cycle().select_related("assignment__assigned_to__person_profile", "repetition").filter(
        workflow_cycle=F("text__current_workflow_cycle"),
    ).annotate(_has_completed_verification=Exists(completed_verification_history())).order_by("-pk")
    assignments = (
        WorkflowRoleAssignment.objects.current_cycle().filter(
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
    author_query = Author.objects.order_by("last_name", "first_name", "pk")
    if not include_authors:
        author_query = author_query.only("pk", "first_name", "last_name")
    queryset = queryset.prefetch_related(Prefetch(
        "authors", queryset=author_query, to_attr="selector_authors",
    ))
    return queryset


from workflow.state import stage_is_open as _is_open, stage_is_active as _is_active, operational_stages


def _terminal(stages):
    return any(stage.stage_type in TERMINAL_STAGES for stage in stages)


def _current_stage(stages, *, prepared=False):
    from workflow.state import current_stage
    return current_stage(stages, prepared=prepared)


def _annotated_texts(*, translated=False, include_translations=False):
    from workflow.state import state_annotations, ORDER
    from workflow.read_queries import exclude_obsolete_verification_placeholders
    from django.db.models.functions import Coalesce
    stages = WorkflowStage.objects.current_cycle().filter(text_id=OuterRef("pk"),
        workflow_cycle=OuterRef("current_workflow_cycle"), is_released=True,
        stage_type__in=ORDER).annotate(**state_annotations())
    stages = exclude_obsolete_verification_placeholders(stages)
    current = stages.filter(is_completed=False, ended_at__isnull=True).order_by('state_priority','-state_order','-iteration','-pk').values('stage_type')[:1]
    last = stages.order_by('-state_order','-iteration','-pk').values('stage_type')[:1]
    query = Text.objects.all() if include_translations else (Text.objects.filter(anthology__is_translated=True) if translated else Text.objects.exclude(anthology__is_translated=True))
    return query.annotate(current_stage_type=Coalesce(Subquery(current), Subquery(last)))


def _text_row(text, include_authors):
    result = _text_data(text, include_authors)
    stages = operational_stages(text.selector_stages)
    current = _current_stage(stages, prepared=True)
    current_data = _stage_data(current)
    result.update(
        current_stage_type=current.stage_type if current else None,
        current_status=current_data,
        current_stages=[
            _stage_data(stage)
            for stage in stages
            if _is_open(stage)
        ],
        current_status_started_at=current.started_at if current else None,
        current_status_ended_at=current.ended_at if current else None,
        last_status_change=getattr(text, "last_status_change", None),
    )
    return result


def text_list_context(*, user, params, scope=None, stage_scope=None, translated=False, include_translations=False):
    require_team_member(user)
    include_authors = can_view_author_data(user)
    anthology_id = _positive_id(params.get("anthology"))
    author_value = params.get("author", "")
    foreign_author_id = _positive_id(author_value[8:]) if include_translations and include_authors and author_value.startswith("foreign:") else None
    author_id = _positive_id(author_value) if include_authors else None
    translator_id = _positive_id(params.get("translator")) if translated and include_authors else None
    valid_statuses = {value for value, _ in active_stage_choices()} | {"none"}
    requested = params.getlist("status") if hasattr(params, "getlist") else [params.get("status", "")]
    statuses = list(dict.fromkeys(v for v in requested if v in valid_statuses))
    status = statuses[-1] if statuses else ""
    query = params.get("q", "").strip()[:500]

    hide_ready = params.get("hide_ready", "0" if translated else "1").strip() != "0"

    def filtered(exclude=None):
        result = _annotated_texts(translated=translated, include_translations=include_translations)
        if scope is not None:
            result = result.filter(pk__in=scope.values("pk"))
        if stage_scope is not None:
            result = result.filter(pk__in=stage_scope.values("text_id")).annotate(current_stage_type=Subquery(stage_scope.filter(text_id=OuterRef("pk")).order_by("pk").values("stage_type")[:1]))
        if anthology_id is not None and exclude != 'anthology':
            result = result.filter(anthology_id=anthology_id)
        if author_id is not None and exclude != 'author':
            condition = Q(translation__foreign_authors__pk=author_id) if translated else Q(authors__pk=author_id)
            result = result.filter(condition)
        if foreign_author_id is not None and exclude != 'author':
            result = result.filter(translation__foreign_authors__pk=foreign_author_id, anthology__is_translated=True)
        if translator_id is not None and exclude != 'translator':
            result = result.filter(translation__translators__pk=translator_id)
        if statuses and exclude != 'status':
            condition = Q(current_stage_type__in=[v for v in statuses if v != 'none'])
            if 'none' in statuses:
                condition |= Q(current_stage_type__isnull=True)
            result = result.filter(condition)
        for term in query.split():
            condition = Q(title__plcontains=term) | Q(anthology__title__plcontains=term)
            if include_authors:
                if translated or include_translations:
                    condition |= Q(translation__foreign_authors__first_name__plcontains=term) | Q(translation__foreign_authors__last_name__plcontains=term) | Q(translation__foreign_authors__pseudonym__plcontains=term)
                    condition |= Q(translation__translators__first_name__plcontains=term) | Q(translation__translators__last_name__plcontains=term) | Q(translation__translators__pseudonym__plcontains=term) | Q(translation__translators__language__plcontains=term)
                if not translated:
                    condition |= Q(authors__first_name__plcontains=term) | Q(authors__last_name__plcontains=term) | Q(authors__pseudonym__plcontains=term)
            result = result.filter(condition)
        if hide_ready:
            result = result.filter(
                Q(current_stage_type__isnull=True)
                | ~Q(current_stage_type__in=(StageType.READY, StageType.WITHDRAWN))
            )
        return result.distinct()

    queryset = filtered()
    anthology_options = Anthology.objects.filter(is_translated=translated).filter(
        Q(pk__in=filtered('anthology').values('anthology_id')) | Q(pk=anthology_id))
    if include_translations:
        anthology_options = Anthology.objects.filter(Q(pk__in=filtered('anthology').values('anthology_id')) | Q(pk=anthology_id))
    elif translated:
        anthology_options = Anthology.objects.filter(is_translated=True)
    author_options = Author.objects.filter(
        Q(pk__in=filtered('author').values('authors__pk')) | Q(pk=author_id)) if include_authors else Author.objects.none()
    if translated and include_authors:
        author_options = ForeignAuthor.objects.filter(Q(pk__in=filtered('author').values('translation__foreign_authors__pk')) | Q(pk=author_id)).distinct()
    translator_options = Translator.objects.filter(Q(pk__in=filtered('translator').values('translation__translators__pk')) | Q(pk=translator_id)) if translated and include_authors else Translator.objects.none()
    available_statuses = set(filtered('status').values_list('current_stage_type', flat=True))

    sort = params.get("sort", "anthology")
    if sort not in TEXT_SORTS:
        sort = "anthology"

    ordering = TEXT_SORTS[sort]
    if scope is None and stage_scope is None:
        from core.models import WorkflowEvent
        history = WorkflowEvent.objects.filter(text_id=OuterRef('pk')).exclude(
            previous_status=F('next_status')).order_by('-created_at', '-pk')
        queryset = queryset.annotate(last_status_change=Subquery(history.values('created_at')[:1]))
        requested_sort = params.get('sort', '')
        if requested_sort in ('last_status_change', '-last_status_change'):
            sort = requested_sort
            direction = F('last_status_change')
            ordering = (direction.desc(nulls_last=True) if sort.startswith('-') else direction.asc(nulls_last=True), 'pk')
    queryset = _prepared_texts(
        queryset.order_by(*ordering),
        include_authors,
    )
    if translated or include_translations:
        queryset = queryset.select_related("translation").prefetch_related("translation__translators", "translation__foreign_authors")
        if translated and params.get('sort', '').lstrip('-') in ('authors', 'translators'):
            relation = 'foreign_authors' if params['sort'].lstrip('-') == 'authors' else 'translators'
            model = ForeignAuthor if relation == 'foreign_authors' else Translator
            first = model.objects.filter(translations__text_id=OuterRef('pk')).order_by('last_name', 'first_name', 'pk')
            queryset = queryset.annotate(_translation_person_last=Subquery(first.values('last_name')[:1]),
                                         _translation_person_first=Subquery(first.values('first_name')[:1]))
            prefix = '-' if params['sort'].startswith('-') else ''
            queryset = queryset.order_by(prefix+'_translation_person_last', prefix+'_translation_person_first', 'pk')
            sort = params['sort']
    mixed_foreign_options = []
    if include_translations and include_authors:
        foreign_options = ForeignAuthor.objects.filter(
            Q(pk__in=filtered('author').filter(anthology__is_translated=True).values('translation__foreign_authors__pk')) | Q(pk=foreign_author_id)).distinct()
        mixed_foreign_options = [dict(_author_data(a), pk=f"foreign:{a.pk}") for a in foreign_options]
    return {
        "filtered_queryset": queryset,
        "texts": _ProjectedRows(
            queryset,
            lambda text: _text_row(text, include_authors),
        ),
        "anthologies": list(
            anthology_options.order_by("title", "pk").values("pk", "title")
        ),
        "filter_authors": (
            [_author_data(author) for author in author_options] + mixed_foreign_options
            if include_authors
            else []
        ),
        "status_choices": [(v, label) for v, label in active_stage_choices() if v in available_statuses or v in statuses],
        "show_no_status_filter": None in available_statuses or "none" in statuses,
        "selected_anthology_id": str(anthology_id) if anthology_id else "",
        "selected_author_id": f"foreign:{foreign_author_id}" if foreign_author_id else str(author_id) if author_id else "",
        "filter_translators": [_author_data(person) for person in translator_options],
        "selected_translator_id": str(translator_id) if translator_id else "",
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
            workflow_role_assignments__role__in=[value for value, _ in active_role_choices()],
        ).distinct().order_by("anthology__title", "title", "pk"),
        include_authors,
    )


def my_texts_context(*, user, selected_view="active", params=None):
    require_team_member(user)
    selected_view = {"waiting": "active", "completed": "all"}.get(selected_view, selected_view)
    if selected_view not in {"active", "all"}:
        selected_view = "active"
    include_authors = can_view_author_data(user)
    today = timezone.localdate()
    from workflow.read_queries import filter_my_texts
    texts = filter_my_texts(_user_texts(user, include_authors), user, selected_view, today)

    params = params.copy() if params is not None else {}
    params.setdefault("hide_ready", "0")
    filters = text_list_context(user=user, params=params, scope=texts, include_translations=True)
    texts = filters.pop("filtered_queryset")
    from workflow.read_queries import annotate_my_work
    texts = annotate_my_work(texts, user, today)
    # Show every own execution, including superseded assignments. This only
    # supplies labels; current work and permissions retain their existing rules.
    texts = texts.prefetch_related(Prefetch(
        "workflow_role_assignments",
        queryset=WorkflowRoleAssignment.objects.filter(
            assigned_to_id=user.pk,
            role__in=[value for value, _ in active_role_choices()],
        ).select_related("assigned_to", "assigned_to__person_profile").order_by(
            "role", "workflow_cycle", "execution_number", "pk",
        ),
        to_attr="selector_own_assignments",
    ))

    def project(text):
        assignments = text.selector_own_assignments
        row = _text_row(text, include_authors)
        row.update(
            user_assignments=[_assignment_data(item) for item in assignments],
            visible_assignments=[_assignment_data(item) for item in assignments],
            current_cycle_stage_history=[
                _stage_data(stage) for stage in text.selector_stages
            ],
            has_active_work=text.work_active,
            has_reserved_work=text.work_waiting,
            has_completed_work=text.work_completed,
            is_waiting_for_other_role=text.work_waiting,
        )
        return row

    filters.update(texts=_ProjectedRows(texts, project), selected_view=selected_view)
    return filters


def user_workflow_summary(user, *, today=None, limit=None):
    require_team_member(user)
    today = today or timezone.localdate()
    from workflow.read_queries import dashboard_querysets
    active, reserved = dashboard_querysets(user, today)
    active, reserved = ordinary(active), ordinary(reserved)
    counts = (active.count(), reserved.count())
    active = active.select_related("text__anthology").prefetch_related("text__authors")
    reserved = reserved.select_related("text__anthology", "assigned_to__person_profile").prefetch_related("text__authors")
    if limit is not None:
        active, reserved = active[:limit], reserved[:limit]
    return {
        "active_stages": [_stage_data(stage, _text_data(stage.text, False)) for stage in active],
        "reserved_assignments": [_assignment_data(item, _text_data(item.text, False)) for item in reserved],
        "active_stage_count": counts[0], "reserved_assignment_count": counts[1],
    }


def available_stages_for_user(*, user, params=None, with_filters=False):
    require_team_member(user)
    from workflow.availability import claim_access
    from workflow.read_queries import available_stages
    include_authors = can_view_author_data(user)
    stages = ordinary(available_stages(user, claim_access(user))).select_related("text__anthology")
    filters = text_list_context(user=user, params=params or {}, stage_scope=stages) if with_filters else None
    if filters is not None:
        filtered = filters.pop("filtered_queryset")
        stages = stages.filter(text_id__in=filtered.values("pk"))
        ordering = [("-" if item.startswith("-") else "") + ("stage_type" if item.lstrip("-") == "current_stage_type" else "text__" + item.lstrip("-")) for item in TEXT_SORTS[filters["sort"]]]
        stages = stages.order_by(*ordering, "pk")
    author_query = Author.objects.order_by("last_name", "first_name", "pk")
    if not include_authors:
        author_query = author_query.only("pk", "first_name", "last_name")
    stages = stages.prefetch_related(Prefetch(
        "text__authors", queryset=author_query, to_attr="selector_authors"))

    def project(stage):
        role = STAGE_ROLE_MAP.get(stage.stage_type)
        text_data = _text_data(stage.text, include_authors)
        row = _stage_data(stage, text_data)
        row.update(required_group="Superuser" if role == Role.STYLING else ROLE_GROUPS.get(role, "Redaktor"),
                   available_role=role)
        return row

    rows = _ProjectedRows(stages, project)
    if with_filters:
        filters.pop("texts", None)
        return rows, filters
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
        if value.strip() in dict(active_stage_choices())
    ))
    query = params.get("q", "").strip()
    hide_ready = params.get("hide_ready", "0").strip() != "0"

    # Wyszukiwanie po autorze nie jest wykonywane dla koordynatora.
    from django.db.models import Q

    stages = WorkflowStage.objects.current_cycle().select_related("assignment__assigned_to__person_profile").filter(
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
    # Paginate texts, never individual stage executions.
    text_query = _annotated_texts()
    if selected_stages:
        text_query = text_query.filter(pk__in=stages.values('text_id'))
    if anthology_ids:
        text_query = text_query.filter(anthology_id__in=anthology_ids)
    for term in query.split():
        condition = Q(title__plcontains=term) | Q(anthology__title__plcontains=term)
        if include_authors:
            condition |= (Q(authors__first_name__plcontains=term) | Q(authors__last_name__plcontains=term)
                          | Q(authors__pseudonym__plcontains=term) | Q(authors__email__plcontains=term))
        text_query = text_query.filter(condition)
    texts = _prepared_texts(text_query.distinct(), include_authors)
    if hide_ready:
        texts = texts.filter(Q(current_stage_type__isnull=True) |
                             ~Q(current_stage_type__in=(StageType.READY, StageType.WITHDRAWN)))
    from django.db.models import Min, Max
    from workflow.read_queries import editorial_approval, exclude_obsolete_verification_placeholders
    texts = texts.annotate(
        summary_started=Min('workflow_stages__started_at'),
        summary_ended=Max('workflow_stages__ended_at'),
        editorial_approved=editorial_approval(
            WorkflowStage.objects.current_cycle().filter(text_id=OuterRef('pk')),
            timezone.localdate(),
        ),
    )
    sorts = {
        'recent': ('-pk',), 'title': ('title', 'pk'), '-title': ('-title', 'pk'),
        'anthology': ('anthology__title', 'title', 'pk'),
        '-anthology': ('-anthology__title', 'title', 'pk'),
        'started_at': (F('summary_started').asc(nulls_last=True), 'pk'),
        '-started_at': (F('summary_started').desc(nulls_last=True), 'pk'),
        'ended_at': (F('summary_ended').asc(nulls_last=True), 'pk'),
        '-ended_at': (F('summary_ended').desc(nulls_last=True), 'pk'),
    }
    sort = params.get('sort', 'anthology')
    if sort not in sorts:
        sort = 'anthology'
    texts = texts.order_by(*sorts[sort]).prefetch_related(Prefetch(
        'workflow_stages',
        queryset=exclude_obsolete_verification_placeholders(
            WorkflowStage.objects.filter(workflow_cycle=F('text__current_workflow_cycle')))
            .select_related('assignment__assigned_to__person_profile')
            .order_by('execution_number', 'iteration', 'pk'),
        to_attr='summary_stages',
    ), Prefetch(
        'workflow_role_assignments',
        queryset=WorkflowRoleAssignment.objects.filter(workflow_cycle=F('text__current_workflow_cycle'))
            .select_related('assigned_to__person_profile').order_by('execution_number', 'pk'),
        to_attr='summary_assignments',
    ))
    role_columns = [(role, 'Redaktor' if role == 'editor' else label)
                    for role, label in workflow_role_choices()]

    def project(text):
        current = _current_stage(text.selector_stages)
        row = _stage_data(current, _text_data(text, include_authors)) if current else _Record(
            text=_text_data(text, include_authors), get_stage_type_display='–')
        from workflow.archive_executions import is_archive_text
        archive = is_archive_text(text) and current is not None and current.stage_type == StageType.READY
        cells = []
        for role, label in role_columns:
            entries = []
            grouped = {}
            for assignment in text.summary_assignments:
                if assignment.role != role or not assignment.assigned_to_id:
                    continue
                entry = grouped.setdefault(assignment.pk if archive else assignment.assigned_to_id, {
                    'user': _user_data(assignment.assigned_to), 'work': [],
                    'execution_label': assignment_label(assignment) if archive else '',
                })
                entry['work'].extend(stage for stage in text.summary_stages
                                     if stage.assignment_id == assignment.pk)
            from django.utils import timezone
            from workflow.state import stage_is_active
            today = timezone.localdate()
            for entry in grouped.values():
                work = entry.pop('work')
                live = [stage for stage in work if stage.is_current and stage.is_released
                        and not stage.is_completed and stage.ended_at is None]
                if any(stage_is_active(stage, today) for stage in live):
                    state, tone = 'W trakcie', 'active'
                elif any(stage.started_at and stage.started_at > today for stage in live):
                    state, tone = 'Zarezerwowano', 'pending'
                elif live:
                    state, tone = 'Oczekuje', 'pending'
                elif work and all(stage.is_completed or stage.ended_at for stage in work):
                    editor_current = role == 'editor' and any(
                        a.role == role and a.is_current and a.assigned_to_id == entry['user']['pk']
                        for a in text.summary_assignments)
                    if editor_current and not text.editorial_approved and not archive:
                        state, tone = 'Oczekuje', 'pending'
                    else:
                        state, tone = 'Zakończone', 'completed'
                else:
                    state, tone = 'Brak bieżącego zadania', 'inactive'
                entry.update(state=state, tone=tone)
                entries.append(entry)
            cells.append({'role': role, 'entries': entries})
        row['role_cells'] = cells
        return row

    return {
        'stages': _ProjectedRows(texts, project),
        'anthologies': list(Anthology.objects.filter(is_translated=False)
                            .order_by('title', 'pk').values('pk', 'title')),
        'selected_stages': selected_stages,
        'stage_choices': [(v, label) for v, label in active_stage_choices() if v in facets['stage']],
        'role_columns': role_columns, 'selected_anthology_ids': anthology_ids,
        'query': query, 'sort': sort, 'hide_ready': hide_ready,
    }




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
    stages = operational_stages(text.selector_stages)
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
    verification_open_started = any(
        stage.stage_type == StageType.FIRST_VERIFICATION and _is_open(stage)
        and stage.started_at is not None for stage in stages
    )
    later_phase = any(
        stage.stage_type not in EDITOR_PHASE_STAGES
        for stage in stages
    )
    can_control = (
        not any(s.repetition_id and not s.is_completed for s in stages) and not terminal and not later_phase
        and (coordinator or owned(Role.EDITOR))
    )
    from workflow.services import completed_stage_exists, editing_follows_first_verification
    first_done = completed_stage_exists(text, StageType.FIRST_VERIFICATION)
    second_done = completed_stage_exists(text, StageType.SECOND_VERIFICATION)
    first_gate = bool(first_done)
    second_gate = bool(second_done)

    can_start_first = bool(
        not any(s.repetition_id and not s.is_completed for s in stages) and not terminal and pending_first and not first_done
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

    stage_rows = _detail_stage_rows(
        user, text, stages, assignments, text_data,
        terminal=terminal, coordinator=coordinator, today=today,
        can_start_first=can_start_first, can_resume=can_resume,
    )

    row_by_id = {row["pk"]: row for row in stage_rows}

    def stage_row(stage):
        return row_by_id.get(stage.pk) if stage else None

    current = _current_stage(stages)
    previous = max(
        (stage for stage in stages if stage.is_completed),
        key=lambda stage: (stage.ended_at or date.min, stage.pk),
        default=None,
    )
    visible = list({stage.pk: stage_row(stage)
                    for stage in (current, previous) if stage is not None}.values())
    visible_ids = {row["pk"] for row in visible}

    archived = []
    if coordinator:
        historical_stages = WorkflowStage.objects.filter(text_id=text.pk).exclude(
            workflow_cycle=text.current_workflow_cycle, is_current=True,
        ).select_related("assignment__assigned_to__person_profile").order_by("-workflow_cycle", "-pk")
        archived = [row for row in stage_rows if row["pk"] not in visible_ids]
        operational_ids = {stage.pk for stage in stages}
        for stage in text.selector_stages:
            if stage.pk not in operational_ids:
                row = _stage_data(stage, text_data)
                row.update(can_start=False, can_complete=False, assigned_user=_user_data(stage.assignment.assigned_to) if stage.assignment and stage.assignment.assigned_to else None)
                archived.append(row)
        for stage in historical_stages:
            row = _stage_data(stage, text_data)
            row.update(can_start=False, can_complete=False, assigned_user=_user_data(stage.assignment.assigned_to) if stage.assignment and stage.assignment.assigned_to else None)
            archived.append(row)

    notes = _text_notes(text, user, coordinator)

    source_data, opinions = _source_review_data(text, user, include_authors)

    open_repetition = text.repetitions.filter(completed_at__isnull=True, canceled_at__isnull=True).first()
    can_cancel_repetition = bool(user.is_superuser and open_repetition and open_repetition.previous_stage_ids
        and not open_repetition.stages.filter(started_at__isnull=False).exists()
        and not open_repetition.stages.filter(is_completed=True).exists()
        and not open_repetition.assignments.filter(assigned_to__isnull=False).exists())
    team = _text_team_members(text, assignments)
    from workflow.availability import claim_access
    from workflow.read_queries import available_stages
    claimable = available_stages(user, claim_access(user)).filter(text_id=text.pk)
    return {
        "text": text_data,
        "user_assignments": [_assignment_data(item) for item in own],
        "stages": stage_rows,
        "claimable_stages": [_stage_data(stage, text_data) for stage in claimable],
        "visible_stages": visible,
        "archived_stages": archived,
        "can_view_stage_history": coordinator,
        "team_members": [member for member in team if not member.get("is_previous")],
        "previous_team_members": [member for member in team if member.get("is_previous")],
        "is_assigned": bool(own),
        "is_read_only": not coordinator and not own,
        "can_add_note": coordinator or bool(own),
        "can_edit_coordinator_note": coordinator,
        "show_coordinator_note": bool(text.coordinator_note.strip()),
        "text_notes": notes,
        "repeat_queue": sorted([row for row in stage_rows if row.get('repetition_id') and not row['is_completed']], key=lambda row: row['queue_position']),
        "open_repetition": open_repetition,
        "can_cancel_repetition": can_cancel_repetition,
        "handoffs": text.workflow_handoffs.select_related('previous_assignment__assigned_to', 'new_assignment__assigned_to', 'stage').order_by('-created_at'),
        "source_review": source_data,
        "handoff_stages": [row for row in stage_rows if not row["is_completed"] and row["is_released"] and row.get("assigned_user")],
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
            can_control and editing and not first_gate
            and not verification_open_started
        ),
        "can_start_first_verification": can_start_first,
        "can_resume_editing": can_resume,
        "can_send_to_author": bool(
            can_control and editing and first_gate and not verification_open
        ),
        "can_send_to_second_verification": bool(
            can_control and editing and first_gate and not second_gate
            and editing_follows_first_verification(text, editing)
            and not any(
                stage.stage_type == StageType.SECOND_VERIFICATION
                for stage in stages
            )
        ),
        "can_finish_editing": bool(
            can_control and editing and second_gate and not verification_open
        ),
        "all_authors": (
            [_author_data(author) for author in text.selector_authors]
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


def _text_notes(text, user, coordinator):
    return [
        {
            "pk": note.pk,
            "author": _user_data(note.author),
            "can_manage": coordinator or note.author_id == user.pk,
            "content": note.content,
            "is_important": note.is_important,
            "created_at": note.created_at,
        }
        for note in TextNote.objects.filter(text_id=text.pk)
        .select_related("author", "author__person_profile")
        .order_by("-created_at", "-pk")
    ]



def _source_review_data(text, user, include_authors):
    source_query = Review.objects.visible_to(user).filter(copied_text_id=text.pk).select_related("reviewers")
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
            can_open=not source.old_reviews or include_authors,
            general_notes=getattr(getattr(source, "reviewers", None), "general_notes", ""),
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
            .select_related("user", "user__person_profile", "historical_person")
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
                        if assignment.user else str(assignment.historical_person) if assignment.historical_person else "Usunięte konto"
                    ),
                    "opinion_value": assignment.opinion,
                    "opinion": assignment.get_opinion_display(),
                    "opinion_display": assignment.get_opinion_display(),
                    "notes": assignment.notes,
                    "status_changed_at": assignment.opinion_changed_at,
                }
            )

    return source_data, opinions


def _text_team_members(text, assignments):
    from workflow.archive_executions import is_archive_text
    if is_archive_text(text) and WorkflowStage.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle,
                                                             stage_type='ready', is_current=True).exists():
        order = {'editor':0, 'editing_coordinator':1, 'editing_reviewer':2,
                 'proofreader_1':3, 'proofreader_2':4, 'proofreader_3':5, 'proofreader_4':6,
                 'verifier_1':7, 'verifier_2':8, 'verifier_3':9,
                 'verification_coordinator':10, 'styling':11}
        members = WorkflowRoleAssignment.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle,
            stages__imported_completed=True, assigned_to__isnull=False).select_related('text','assigned_to__person_profile').distinct()
        return [{'role':item.role, 'label':assignment_label(item), 'assignment':_assignment_data(item),
                 'is_assigned':True, 'archive_execution':True, 'user':_user_data(item.assigned_to)}
                for item in sorted(members,key=lambda a:(order.get(a.role,99),a.execution_number,a.pk))]
    team_role_order = {role: i for i, (role, _) in enumerate(workflow_role_choices())}
    return sorted([
            {
                "role": role,
                "label": assignment_label(assignments[role]) if role in assignments else label,
                "assignment": _assignment_data(assignments.get(role)),
                "user": (
                    _user_data(assignments[role].assigned_to)
                    if role in assignments else None
                ),
                "is_assigned": bool(
                    role in assignments and assignments[role].assigned_to_id
                ),
            }
            for role, label in workflow_role_choices() if role != Role.STYLING
        ] + [
            {"role": item.role, "label": assignment_label(item, show_first=True), "archive_execution": is_archive_text(text),
             "is_previous": True, "is_assigned": True, "workflow_cycle": item.workflow_cycle,
             "user": _user_data(item.assigned_to)}
            for item in WorkflowRoleAssignment.objects.filter(text=text, assigned_to__isnull=False).exclude(role__in=(*IMPORT_ONLY_ROLES, Role.STYLING)).exclude(
                workflow_cycle=text.current_workflow_cycle, is_current=True).select_related('assigned_to__person_profile').order_by('workflow_cycle','role','execution_number')
        ], key=lambda member: (team_role_order.get(member["role"], 999), bool(member.get("is_previous"))))


def _detail_stage_rows(user, text, stages, assignments, text_data, *, terminal,
                       coordinator, today, can_start_first, can_resume):
    def owned(role):
        assignment = assignments.get(role)
        return bool(assignment and assignment.assigned_to_id == user.pk)

    from core.selectors.people import user_leave_information
    leave_cache = {}
    stage_rows = []
    for stage in stages:
        role = STAGE_ROLE_MAP.get(stage.stage_type)
        assignment = stage.assignment or assignments.get(role)
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
            and stage.stage_type not in TERMINAL_STAGES
            and (stage.stage_type != StageType.AUTHOR_EDITING or (text.current_workflow_cycle > 1 and len(stages) == 1))
        )
        if stage.stage_type == StageType.FIRST_VERIFICATION:
            can_start = can_start_first
        elif stage.stage_type == StageType.EDITING:
            can_start = can_start and can_resume

        if stage.stage_type == StageType.STYLING:
            can_start = can_start and user.is_superuser and bool(assigned_user and assigned_user.is_superuser)
        row.update(
            assigned_role=role,
            assigned_user=_user_data(assigned_user),
            leave_information=leave_cache.get(assigned_user.pk) if assigned_user else None,
            assigned_person=_person_data(
                getattr(assigned_user, "person_profile", None)
                if assigned_user else None
            ),
            can_start=can_start,
            can_edit_schedule=bool(not terminal and may_act and assigned_user and _is_open(stage) and stage.started_at and stage.started_at > today and (stage.stage_type != StageType.STYLING or user.is_superuser)),
            can_complete=bool(
                not terminal and role and may_act
                and (stage.stage_type != StageType.STYLING or user.is_superuser)
                and _is_active(stage, today)
                and stage.stage_type not in TERMINAL_STAGES | {
                    StageType.READY_FOR_EDITING,
                    StageType.EDITING,
                    StageType.AUTHOR_EDITING,
                }
            ),
        )
        from people.leave_access import is_on_leave
        from workflow.services import user_can_complete_stage
        available = stage.is_released and stage.is_current
        if stage.repetition_id:
            row['can_start'] = bool(available and not terminal and assigned_user and may_act and _is_open(stage) and not stage.started_at)
            row['can_complete'] = user_can_complete_stage(stage, user)
        if not available or (assigned_user and is_on_leave(assigned_user)):
            row['can_start'] = False
        row['can_start'] = row['can_start'] and available
        from workflow.services import can_skip_fourth
        row['can_skip'] = can_skip_fourth(stage, user)
        row['is_queued'] = not available
        stage_rows.append(row)

    return stage_rows
