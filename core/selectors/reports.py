from datetime import timedelta

from django import forms
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Subquery
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
        .filter(
            Q(roles__name__iexact=role_name)
            | Q(user__groups__name__iexact=role_name)
        )
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
            Anthology.objects.order_by("title", "pk").values("pk", "title")
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

    valid_stages = {value for value, _ in WorkflowStage.StageType.choices}
    valid_roles = {value for value, _ in WorkflowRoleAssignment.Role.choices}

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
    if not valid:
        return context

    # Historia obejmuje wszystkie cykle. Przydział dopasowujemy
    # do cyklu konkretnego etapu, nigdy do obecnego cyklu tekstu.
    stages = WorkflowStage.objects.filter(
        stage_type__in=stage_roles,
    ).select_related("text", "text__anthology")

    if filters.get("date_from"):
        stages = stages.filter(ended_at__gte=filters["date_from"])
    if filters.get("date_to"):
        stages = stages.filter(ended_at__lte=filters["date_to"])

    assignments = (
        WorkflowRoleAssignment.objects.filter(role__in=set(stage_roles.values()))
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
    role_labels = dict(WorkflowRoleAssignment.Role.choices)
    rows = []

    for stage in stages.iterator(chunk_size=BATCH_SIZE):
        text = stage.text
        role = stage_roles[stage.stage_type]

        assignment = next(
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

        if not _matches(
            filters["q"],
            anthology_title,
            text.title,
            authors,
            role_label,
            person_name,
        ):
            continue

        rows.append(
            {
                "stage_id": stage.pk,
                "workflow_cycle": stage.workflow_cycle,
                "iteration": stage.iteration,
                "anthology_title": anthology_title,
                "anthology_id": text.anthology_id,
                "text_id": text.pk,
                "title": text.title,
                "authors": authors,
                "role": role_label,
                "person_id": person.pk if person else None,
                "person_name": person_name,
                "assigned_at": assignment.assigned_at if assignment else None,
                "started_at": stage.started_at,
                "ended_at": stage.ended_at,
                "is_completed": stage.is_completed,
            }
        )

    from core.selectors.history import historical_assignments
    role_family = {"Redaktor": "editor", "Korektor": "proofreader", "Weryfikator": "verifier"}.get(people_role_name)
    history = historical_assignments().filter(role__in=[role_family] + (["editing_verifier"] if role_family == "verifier" else []))
    if filters.get("date_from"):
        history = history.none()
    if filters.get("date_to"):
        history = history.none()
    if include_authors:
        history = history.prefetch_related(_author_prefetch())
    for item in history:
        text = item.text
        authors = _authors_display(text, include_authors)
        anthology_title = text.anthology.title if text.anthology_id else ""
        if not _matches(filters["q"], anthology_title, text.title, authors, item.role_label, item.display_name):
            continue
        rows.append(dict(stage_id=None, workflow_cycle=None, iteration=None,
                         anthology_title=anthology_title, anthology_id=text.anthology_id,
                         text_id=text.pk, title=text.title, authors=authors, role=item.role_label,
                         person_id=item.person_id, person_name=item.display_name, assigned_at=None,
                         started_at=None, ended_at=None,
                         is_completed=item.is_completed, is_historical=True))
        if item.person_id and not any(p["pk"] == item.person_id for p in context[people_context_name]):
            context[people_context_name].append(_NamedRecord(pk=item.person_id, display_name=item.display_name))
    return _cascade_activity(context, rows, filters, people_context_name)


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

    context.update(
        selected_status=selected_status,
        selected_statuses=selected_statuses,
        status_choices=Review.Status.choices,
    )
    if not valid:
        return context

    # Nie filtrujemy historii po aktualnej roli ani aktywności osoby.
    # Usunięcie konta również nie usuwa informacji o oddanej opinii.
    assignments = ReviewAssignment.objects.filter(
        review__old_reviews=False,
        review__is_hidden=False,
    ).select_related(
        "review",
        "review__anthology",
        "user",
        "user__person_profile",
    )

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
    rows = []

    for assignment in assignments.iterator(chunk_size=BATCH_SIZE):
        review = assignment.review
        person, reviewer_name = _person_and_name(assignment.user)
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

        if not _matches(
            filters["q"],
            anthology_title,
            review.title,
            author_name,
            reviewer_name,
            opinion,
            review_status,
        ):
            continue

        rows.append(
            {
                "assignment_id": assignment.pk,
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
        )

    return _cascade_activity(context, rows, filters, 'reviewers', selected_statuses)


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
        value for value, _ in WorkflowStage.StageType.choices
    }
    selected_stages = list(
        dict.fromkeys(
            value
            for value in params.getlist("stage")
            if value in valid_stage_types
        )
    )

    terminal_stage = WorkflowStage.objects.filter(
        text_id=OuterRef("text_id"),
        workflow_cycle=OuterRef("workflow_cycle"),
        stage_type__in=TERMINAL_STAGES,
    )
    latest_completion = (
        WorkflowStage.objects.filter(
            text_id=OuterRef("text_id"),
            workflow_cycle=OuterRef("workflow_cycle"),
            is_completed=True,
            ended_at__isnull=False,
            ended_at__lte=today,
        )
        .order_by("-ended_at", "-pk")
        .values("ended_at")[:1]
    )

    stages = (
        WorkflowStage.objects.filter(
            workflow_cycle=F("text__current_workflow_cycle"),
            is_completed=False,
            ended_at__isnull=True,
        )
        .annotate(
            report_terminal=Exists(terminal_stage),
            report_waiting_since=Subquery(latest_completion),
        )
        .filter(report_terminal=False)
        .select_related("text", "text__anthology")
        .order_by("text__title", "stage_type", "pk")
    )
    if selected_stages:
        stages = stages.filter(stage_type__in=selected_stages)
    if include_authors:
        stages = stages.prefetch_related(_author_prefetch())

    active_limit = today - timedelta(days=active_days)
    waiting_limit = today - timedelta(days=waiting_days)

    active_condition = Q(started_at__lte=active_limit)
    waiting_condition = Q(
        started_at__isnull=True,
        report_waiting_since__lte=waiting_limit,
    )

    if mode == "active":
        stages = stages.filter(active_condition)
    elif mode == "waiting":
        stages = stages.filter(waiting_condition)
    else:
        stages = stages.filter(active_condition | waiting_condition)

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
            stage.started_at
            if inactivity_type == "active"
            else stage.report_waiting_since
        )
        if since is None:
            continue

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
            "get_stage_type_display": stage.get_stage_type_display(),
            "iteration": stage.iteration,
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
                "days": (today - since).days,
            }
        )

    rows.sort(
        key=lambda row: (
            -row["days"],
            row["text"]["title"].casefold(),
            row["stage"]["pk"],
        )
    )

    return {
        "rows": rows,
        "query": query,
        "mode": mode,
        "stage_choices": WorkflowStage.StageType.choices,
        "selected_stages": selected_stages,
        "today": today,
    }

def _cascade_activity(context, rows, filters, people_key, statuses=None):
    dimensions = {
        'anthology': ('anthology_id', [filters['anthology']] if filters.get('anthology') else []),
        'person': ('person_id', [filters['person']] if filters.get('person') else []),
    }
    if statuses is not None:
        dimensions['status'] = ('review_status_value', statuses)
    def matches(row, exclude=None):
        return all(not values or row[field] in values for name, (field, values) in dimensions.items() if name != exclude)
    options = {name: {row[field] for row in rows if matches(row, name)} | set(values)
               for name, (field, values) in dimensions.items()}
    context['anthologies'] = [item for item in context['anthologies'] if item['pk'] in options['anthology']]
    context[people_key] = [item for item in context[people_key] if item['pk'] in options['person']]
    if statuses is not None:
        context['status_choices'] = [(v, label) for v, label in Review.Status.choices if v in options['status']]
    context['activity_rows'] = [row for row in rows if matches(row)]
    return context
