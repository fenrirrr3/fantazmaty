from core.translation_scope import ordinary
from workflow.catalog import active_stage_choices, active_role_choices
from workflow.labels import execution_label, WORK_LABELS
from datetime import timedelta

from django import forms
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Subquery, Case, When, Value, CharField, BigIntegerField
from django.db.models.functions import Cast, Coalesce, Concat, NullIf, Trim
from django.utils import timezone

from authors.models import Author
from core.permissions import can_view_author_data, require_coordinator
from people.models import Person
from texts.models import Anthology, Review, ReviewAssignment
from workflow.models import WorkflowRoleAssignment, WorkflowStage


MAX_DATABASE_ID = 9_223_372_036_854_775_807
BATCH_SIZE = 200

TERMINAL_STAGES = (
    WorkflowStage.StageType.READY,
    WorkflowStage.StageType.WITHDRAWN,
)


class _NamedRecord(dict):
    def __str__(self):
        return self.get("display_name", "")


class _ActivityFilters(forms.Form):
    q = forms.CharField(required=False, max_length=500)
    anthology = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_DATABASE_ID,
    )
    person = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_DATABASE_ID,
    )
    date_from = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    date_to = forms.DateField(required=False, input_formats=["%Y-%m-%d"])

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")

        if date_from and date_to and date_to < date_from:
            raise forms.ValidationError(
                "Data końcowa nie może poprzedzać daty początkowej."
            )

        return cleaned


def _people_for_role(role_name):
    people = (
        Person.objects.filter(
            is_active=True,
            user__isnull=False,
            user__is_active=True,
        )
        .filter(roles__name__iexact=role_name)
        .distinct()
        .order_by("last_name", "first_name", "pk")
    )

    return [
        _NamedRecord(
            pk=person.pk,
            first_name=person.first_name,
            last_name=person.last_name,
            display_name=str(person),
        )
        for person in people
    ]


def _filter_context(params, *, role_name, people_context_name):
    form = _ActivityFilters(params)
    valid = form.is_valid()
    cleaned = form.cleaned_data

    context = {
        "activity_rows": [],
        "filter_form": form,
        "filter_errors": form.errors,
        "anthologies": list(
            ordinary(Anthology.objects).order_by("title", "pk").values("pk", "title")
        ),
        people_context_name: _people_for_role(role_name),
        "query": cleaned.get("q", ""),
        "selected_anthology_id": cleaned.get("anthology"),
        "selected_person_id": cleaned.get("person"),
        "date_from": (
            cleaned["date_from"].isoformat()
            if cleaned.get("date_from")
            else ""
        ),
        "date_to": (
            cleaned["date_to"].isoformat()
            if cleaned.get("date_to")
            else ""
        ),
    }
    return context, cleaned, valid


def _matches(query, *values):
    from core.search_lookup import fold_polish
    searchable = fold_polish(" ".join(str(value or "") for value in values))
    return all(fold_polish(term) in searchable for term in query.split())


def _author_prefetch():
    return Prefetch(
        "text__authors",
        queryset=Author.objects.order_by("last_name", "first_name", "pk"),
        to_attr="report_authors",
    )


def _authors_display(text, include_authors):
    if not include_authors:
        return ""
    return ", ".join(str(author) for author in text.report_authors)


def _person_and_name(user):
    if user is None:
        return None, "Usunięte konto"

    person = getattr(user, "person_profile", None)
    name = user.get_full_name() or "Nieuzupełnione dane"

    return person, name


def workflow_activity_context(
    *,
    user,
    params,
    stage_roles,
    people_role_name,
    people_context_name,
):
    require_coordinator(user)
    include_authors = can_view_author_data(user)

    valid_stages = {value for value, _ in active_stage_choices()}
    valid_roles = {value for value, _ in active_role_choices()}

    if any(
        stage not in valid_stages or role not in valid_roles
        for stage, role in stage_roles.items()
    ):
        raise ValueError("Nieprawidłowa mapa etapów i ról raportu.")

    context, filters, valid = _filter_context(
        params,
        role_name=people_role_name,
        people_context_name=people_context_name,
    )
    context['_include_authors'] = include_authors
    if not valid:
        return context

    # Historia obejmuje wszystkie cykle. Przydział dopasowujemy
    # do cyklu konkretnego etapu, nigdy do obecnego cyklu tekstu.
    stages = ordinary(WorkflowStage.objects).filter(
        stage_type__in=stage_roles,
    ).select_related("text", "text__anthology", "assignment__assigned_to__person_profile")

    if filters.get("date_from"):
        stages = stages.filter(ended_at__gte=filters["date_from"])
    if filters.get("date_to"):
        stages = stages.filter(ended_at__lte=filters["date_to"])

    assignments = (
        ordinary(WorkflowRoleAssignment.objects).filter(role__in=set(stage_roles.values()))
        .select_related("assigned_to", "assigned_to__person_profile")
        .order_by("workflow_cycle", "role", "pk")
    )
    stages = stages.prefetch_related(
        Prefetch(
            "text__workflow_role_assignments",
            queryset=assignments,
            to_attr="report_assignments",
        )
    )
    if include_authors:
        stages = stages.prefetch_related(_author_prefetch())

    stages = stages.order_by(
        F("ended_at").desc(nulls_last=True),
        F("started_at").desc(nulls_last=True),
        "text__title",
        "stage_type",
        "-pk",
    )
    role_labels = dict(active_role_choices())
    stage_role = Case(*[When(stage_type=kind, then=Value(role)) for kind, role in stage_roles.items()], output_field=CharField())
    # The expected role expression belongs to the outer Stage, not Assignment.
    stages = stages.annotate(report_role=stage_role)
    fallback = ordinary(WorkflowRoleAssignment.objects).filter(text_id=OuterRef('text_id'), workflow_cycle=OuterRef('workflow_cycle'), role=OuterRef('report_role')).order_by('pk')
    stages = stages.annotate(
        report_person_id=Case(When(assignment__isnull=False, then=F('assignment__assigned_to__person_profile__pk')), default=Subquery(fallback.values('assigned_to__person_profile__pk')[:1]), output_field=BigIntegerField()),
        report_first=Case(When(assignment__isnull=False, then=F('assignment__assigned_to__first_name')), default=Subquery(fallback.values('assigned_to__first_name')[:1])),
        report_last=Case(When(assignment__isnull=False, then=F('assignment__assigned_to__last_name')), default=Subquery(fallback.values('assigned_to__last_name')[:1])),
        report_assigned_at=Case(When(assignment__isnull=False, then=F('assignment__assigned_at')), default=Subquery(fallback.values('assigned_at')[:1])),
        report_user_id=Case(When(assignment__isnull=False, then=F('assignment__assigned_to_id')), default=Subquery(fallback.values('assigned_to_id')[:1]), output_field=BigIntegerField()),
        report_role_label=Case(*[
            When(stage_type=kind, execution_number__gt=1, then=Concat(
                Value(WORK_LABELS.get(role, role_labels.get(role, role))),
                Value(' (wyk. '), Cast('execution_number', CharField()), Value(')')))
            for kind, role in stage_roles.items()
        ], *[When(stage_type=kind, then=Value(role_labels.get(role, role))) for kind, role in stage_roles.items()], output_field=CharField()),
    ).annotate(report_person_name=Case(
        When(report_user_id__isnull=True, report_assigned_at__isnull=True, then=Value('Nie przypisano')),
        When(report_user_id__isnull=True, then=Value('Usunięte konto')),
        default=Coalesce(NullIf(Trim(Concat(Coalesce('report_first', Value('')), Value(' '), Coalesce('report_last', Value('')))), Value('')), Value('Nieuzupełnione dane')),
        output_field=CharField(),
    ))
    query_fields = ['text__title', 'text__anthology__title', 'report_person_name', 'report_role_label']
    if include_authors: query_fields += ['text__authors__first_name', 'text__authors__last_name', 'text__authors__pseudonym']
    stages = _report_search(stages, filters['q'], query_fields).distinct()
    def project(stage):
        text = stage.text
        role = stage_roles[stage.stage_type]

        assignment = stage.assignment or next(
            (
                item
                for item in text.report_assignments
                if item.workflow_cycle == stage.workflow_cycle
                and item.role == role
            ),
            None,
        )
        assigned_user = assignment.assigned_to if assignment else None
        person, person_name = _person_and_name(assigned_user)

        if assignment is None:
            person_name = "Nie przypisano"
        elif assignment.assigned_to_id is None and assignment.assigned_at is None:
            person_name = "Nie przypisano"

        authors = _authors_display(text, include_authors)
        anthology_title = text.anthology.title if text.anthology else ""
        role_label = role_labels.get(role, role)
        if stage.execution_number > 1:
            role_label = execution_label(WORK_LABELS.get(role, role_label), stage.execution_number)

        return {
                "stage_id": stage.pk,
                "workflow_cycle": stage.workflow_cycle,
                "iteration": stage.iteration,
                "execution_number": stage.execution_number,
                "anthology_title": anthology_title,
                "anthology_id": text.anthology_id,
                "text_id": text.pk,
                "title": text.title,
                "authors": authors,
                "role": role_label,
                "role_code": role,
                "person_id": person.pk if person else None,
                "person_name": person_name,
                "assigned_at": assignment.assigned_at if assignment else None,
                "started_at": stage.started_at,
                "ended_at": stage.ended_at,
                "is_completed": stage.is_completed,
            }

    return _cascade_sql(context, stages, project, filters, people_context_name, workflow=True)

def reviewer_activity_context(*, user, params):
    require_coordinator(user)
    include_authors = can_view_author_data(user)

    context, filters, valid = _filter_context(
        params,
        role_name="Recenzent",
        people_context_name="reviewers",
    )
    requested = params.getlist("status") if hasattr(params, "getlist") else [params.get("status", "")]
    selected_statuses = [v for v in requested if v in Review.Status.values]
    selected_status = selected_statuses[-1] if selected_statuses else ""

    archive = params.get("archive", "current")
    if archive not in {"current", "archived", "all"}:
        archive = "current"
    context["selected_archive"] = archive
    context.update(
        selected_status=selected_status,
        selected_statuses=selected_statuses,
        status_choices=Review.Status.choices,
    )
    context['_include_authors'] = include_authors
    if not valid:
        return context

    # Nie filtrujemy historii po aktualnej roli ani aktywności osoby.
    # Usunięcie konta również nie usuwa informacji o oddanej opinii.
    assignments = ordinary(ReviewAssignment.objects).submitted().select_related(
        "review",
        "review__anthology",
        "user",
        "user__person_profile", "historical_person",
    ).filter(Q(review__old_reviews=True) | Q(review__is_hidden=False))
    if archive != "all":
        assignments = assignments.filter(review__old_reviews=(archive == "archived"))

    if filters.get("date_from"):
        assignments = assignments.filter(
            opinion_changed_at__gte=filters["date_from"],
        )
    if filters.get("date_to"):
        assignments = assignments.filter(
            opinion_changed_at__lte=filters["date_to"],
        )

    assignments = assignments.order_by(
        F("opinion_changed_at").desc(nulls_last=True),
        "review__title",
        "review_id",
        "position",
        "pk",
    )
    assignments = assignments.annotate(
        report_person_id=Coalesce('historical_person_id', 'user__person_profile__pk', output_field=BigIntegerField()),
        report_first=Coalesce('historical_person__first_name', 'user__first_name', Value('')),
        report_last=Coalesce('historical_person__last_name', 'user__last_name', Value('')),
        report_opinion_label=Case(*[When(opinion=value, then=Value(str(label))) for value, label in ReviewAssignment._meta.get_field('opinion').choices], output_field=CharField()),
        report_status_label=Case(*[When(review__status=value, then=Value(str(label))) for value, label in Review.Status.choices], output_field=CharField()),
    ).annotate(report_person_name=Coalesce(
        NullIf(Trim(Concat('report_first', Value(' '), 'report_last')), Value('')),
        Case(When(user_id__isnull=True, historical_person_id__isnull=True, then=Value('Usunięte konto')), default=Value('Nieuzupełnione dane')),
        output_field=CharField(),
    ))
    query_fields = ['review__title', 'review__anthology__title', 'report_person_name', 'report_opinion_label', 'report_status_label']
    if include_authors: query_fields += ['review__author_first_name', 'review__author_last_name']
    assignments = _report_search(assignments, filters['q'], query_fields).distinct()
    person_ids = assignments.values_list('report_person_id', flat=True)
    context['reviewers'] = [_NamedRecord(pk=person.pk, first_name=person.first_name, last_name=person.last_name, display_name=str(person)) for person in Person.objects.filter(pk__in=person_ids).order_by('last_name', 'first_name', 'pk')]
    def project(assignment):
        review = assignment.review
        person, reviewer_name = _person_and_name(assignment.user)
        if assignment.historical_person_id:
            person = assignment.historical_person
            reviewer_name = str(person)
        anthology_title = review.anthology.title if review.anthology else ""
        author_name = (
            " ".join(
                part
                for part in (
                    review.author_first_name,
                    review.author_last_name,
                )
                if part
            )
            if include_authors
            else ""
        )
        opinion = assignment.get_opinion_display()
        review_status = review.get_status_display()

        return {
                "assignment_id": assignment.pk,
                "is_archived": review.old_reviews,
                "anthology_title": anthology_title,
                "anthology_id": review.anthology_id,
                "review_id": review.pk,
                "title": review.title,
                "author_name": author_name,
                "slot": f"Recenzent {assignment.position}",
                "position": assignment.position,
                "person_id": person.pk if person else None,
                "reviewer_name": reviewer_name,
                "opinion": opinion,
                "opinion_value": assignment.opinion,
                "opinion_at": assignment.opinion_changed_at,
                "assigned_at": assignment.assigned_at,
                "review_status": review_status,
                "review_status_value": review.status,
                "decision_at": review.decision_at,
            }


    return _cascade_sql(context, assignments, project, filters, 'reviewers', statuses=selected_statuses)

def workflow_inactivity_context(
    *,
    user,
    params,
    today=None,
    active_days=28,
    waiting_days=7,
):
    require_coordinator(user)
    include_authors = can_view_author_data(user)
    today = today or timezone.localdate()

    if (
        type(active_days) is not int
        or type(waiting_days) is not int
        or active_days < 1
        or waiting_days < 1
    ):
        raise ValueError("Progi przestoju muszą być dodatnimi liczbami dni.")

    query = params.get("q", "").strip()
    mode = params.get("mode", "all").strip()
    if mode not in {"all", "active", "waiting"}:
        mode = "all"

    valid_stage_types = {
        value for value, _ in active_stage_choices()
    }
    selected_stages = list(
        dict.fromkeys(
            value
            for value in params.getlist("stage")
            if value in valid_stage_types
        )
    )

    stages = _inactivity_stages(today, active_days, waiting_days, mode, selected_stages, include_authors)
    rows = _inactivity_rows(stages, query, include_authors, today)

    rows.sort(
        key=lambda row: (
            -(row["days"] if row["days"] is not None else -1),
            row["text"]["title"].casefold(),
            row["stage"]["pk"],
        )
    )

    return {
        "rows": rows,
        "query": query,
        "mode": mode,
        "stage_choices": active_stage_choices(),
        "selected_stages": selected_stages,
        "today": today,
    }


class _ReportRows:
    def __init__(self, queryset, projector, fields):
        self.queryset, self.projector, self.fields = queryset, projector, fields
    def count(self): return self.queryset.count()
    def __len__(self): return self.count()
    def __iter__(self):
        for item in self.queryset.iterator(chunk_size=BATCH_SIZE): yield self.projector(item)
    def __getitem__(self, key):
        if isinstance(key, slice): return [self.projector(item) for item in self.queryset[key]]
        return self.projector(self.queryset[key])
    def sort_table(self, request):
        from core.sort_keys import sql_text_key
        selected = request.GET.get('sort', '')
        key = selected.lstrip('-')
        entry = next((spec for label, spec in self.fields.items() if spec[0] == key), None)
        if not entry: return self, {label:spec[0] for label,spec in self.fields.items()}
        _, field, textual = entry
        query = self.queryset
        if textual:
            query = query.annotate(report_sort=sql_text_key(field, query.db)); field = 'report_sort'
        order = F(field).desc(nulls_last=True) if selected.startswith('-') else F(field).asc(nulls_last=True)
        return _ReportRows(query.order_by(order, 'pk'), self.projector, self.fields), {label:spec[0] for label,spec in self.fields.items()}


def _report_search(queryset, query, fields):
    for term in query.split():
        condition = Q()
        for field in fields: condition |= Q(**{field + '__plcontains': term})
        queryset = queryset.filter(condition)
    return queryset


def _cascade_sql(context, base, project, filters, people_key, workflow=False, statuses=None):
    anthology = 'text__anthology_id' if workflow else 'review__anthology_id'
    def filtered(exclude=None):
        query = base
        if exclude != 'anthology' and filters.get('anthology'): query = query.filter(**{anthology: filters['anthology']})
        if exclude != 'person' and filters.get('person'): query = query.filter(report_person_id=filters['person'])
        if exclude != 'status' and statuses: query = query.filter(review__status__in=statuses)
        return query
    anthology_ids = set(filtered('anthology').values_list(anthology, flat=True))
    person_ids = set(filtered('person').values_list('report_person_id', flat=True))
    if filters.get('anthology'): anthology_ids.add(filters['anthology'])
    if filters.get('person'): person_ids.add(filters['person'])
    context['anthologies'] = [item for item in context['anthologies'] if item['pk'] in anthology_ids]
    context[people_key] = [item for item in context[people_key] if item['pk'] in person_ids]
    if statuses is not None:
        available = set(filtered('status').values_list('review__status', flat=True)) | set(statuses)
        context['status_choices'] = [(value, label) for value, label in Review.Status.choices if value in available]
    fields = {'Antologia': ('anthology', 'text__anthology__title' if workflow else 'review__anthology__title', True),
              'Tytuł': ('title', 'text__title' if workflow else 'review__title', True)}
    if workflow:
        fields.update({'Osoba': ('person','report_person_name',True), 'Korektor': ('person','report_person_name',True),
                      'Weryfikator': ('person','report_person_name',True), 'Redaktor': ('person','report_person_name',True),
                      'Rola': ('role','report_role_label',True), 'Rozpoczęcie': ('started_at','started_at',False),
                      'Zakończenie': ('ended_at','ended_at',False), 'Stan etapu': ('completed','is_completed',False)})
    else:
        fields.update({'Recenzent': ('person','report_person_name',True), 'Przydział': ('position','position',False),
                      'Opinia': ('opinion','report_opinion_label',True), 'Data oceny': ('opinion_at','opinion_changed_at',False),
                      'Status zgłoszenia': ('status','report_status_label',True), 'Data decyzji': ('decision','review__decision_at',False)})
    if context.get('_include_authors'):
        # Author display consists of multiple authors, so this uses their names as a stable SQL key.
        if workflow:
            base = base.annotate(report_author_sort=Subquery(Author.objects.filter(texts__pk=OuterRef('text_id')).order_by('last_name','first_name').values('last_name')[:1]))
            fields['Autorzy'] = ('authors','report_author_sort',True)
        else:
            fields['Autor'] = ('author','review__author_last_name',True)
    context['activity_rows'] = _ReportRows(filtered(), project, fields)
    return context


def _inactivity_stages(today, active_days, waiting_days, mode, selected_stages, include_authors):
    from workflow.read_queries import exclude_obsolete_verification_placeholders

    terminal_stage = ordinary(WorkflowStage.objects).current_cycle().filter(
        text_id=OuterRef("text_id"),
        workflow_cycle=OuterRef("workflow_cycle"),
        stage_type__in=TERMINAL_STAGES,
    )

    stages = (
        ordinary(WorkflowStage.objects).current_cycle().filter(
            workflow_cycle=F("text__current_workflow_cycle"),
            is_completed=False,
            ended_at__isnull=True,
            is_released=True,
        )
        .annotate(
            report_terminal=Exists(terminal_stage),
        )
        .filter(report_terminal=False)
        .select_related("text", "text__anthology")
        .order_by("text__title", "stage_type", "pk")
    )
    stages = exclude_obsolete_verification_placeholders(stages)
    from workflow.waiting import annotate_inactivity_clocks
    stages = annotate_inactivity_clocks(stages, today)
    if selected_stages:
        stages = stages.filter(stage_type__in=selected_stages)
    if include_authors:
        stages = stages.prefetch_related(_author_prefetch())

    active_limit = today - timedelta(days=active_days)
    waiting_limit = today - timedelta(days=waiting_days)

    active_condition = Q(started_at__lte=today, report_active_since__lte=active_limit)
    waiting_condition = Q(started_at__isnull=True) & (
        Q(report_waiting_since__lte=waiting_limit) | Q(report_waiting_since__isnull=True))

    if mode == "active":
        stages = stages.filter(active_condition)
    elif mode == "waiting":
        stages = stages.filter(waiting_condition)
    else:
        stages = stages.filter(active_condition | waiting_condition)

    return stages


def _inactivity_rows(stages, query, include_authors, today):
    rows = []

    for stage in stages.iterator(chunk_size=BATCH_SIZE):
        text = stage.text
        authors = _authors_display(text, include_authors)
        anthology_title = text.anthology.title if text.anthology else ""

        if not _matches(
            query,
            text.title,
            anthology_title,
            authors,
            stage.get_stage_type_display(),
        ):
            continue

        inactivity_type = "active" if stage.started_at is not None else "waiting"
        since = (
            stage.report_active_since
            if inactivity_type == "active"
            else stage.report_waiting_since
        )
        text_data = {
            "pk": text.pk,
            "title": text.title,
            "anthology": (
                {"pk": text.anthology_id, "title": anthology_title}
                if text.anthology_id is not None
                else None
            ),
        }
        stage_data = {
            "pk": stage.pk,
            "text_id": text.pk,
            "text": text_data,
            "workflow_cycle": stage.workflow_cycle,
            "stage_type": stage.stage_type,
            "get_stage_type_display": execution_label(stage.get_stage_type_display(), stage.execution_number),
            "iteration": stage.iteration,
                "execution_number": stage.execution_number,
            "started_at": stage.started_at,
            "ended_at": stage.ended_at,
            "is_completed": stage.is_completed,
        }
        rows.append(
            {
                "stage": stage_data,
                "text": text_data,
                "authors": authors,
                "inactivity_type": inactivity_type,
                "since": since,
                "days": max(0, (today - since).days) if since is not None else None,
            }
        )

    return rows
