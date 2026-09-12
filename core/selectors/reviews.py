from core.filtering import facet_queryset
from django.db.models import Count, F, Prefetch, Q
from django.utils import timezone

from core.permissions import (
    can_self_assign_reviews,
    can_view_author_data,
    require_team_member,
)
from people.models import Vacation
from texts.models import Anthology, Review, ReviewAssignment, Reviewers


MAX_DATABASE_ID = 9_223_372_036_854_775_807
DEFAULT_STATUSES = (Review.Status.NEW, Review.Status.IN_REVIEW)
OPEN_STATUSES = frozenset(DEFAULT_STATUSES)
READING_OPINIONS = ("", Reviewers.Opinion.READING)
MAX_REVIEWERS = ReviewAssignment.MAX_REVIEWERS

ASSIGNMENT_STATES = frozenset(
    {"none", "one", "full", "all_finished", "awaiting_decision"}
)

SORT_FIELDS = {
    "newest": ("-pk",),
    "oldest": ("pk",),
    "title": ("title", "pk"),
    "-title": ("-title", "-pk"),
    "anthology": ("anthology__title", "title", "pk"),
    "-anthology": ("-anthology__title", "title", "pk"),
    "length": ("length", "pk"),
    "-length": ("-length", "-pk"),
    "status": ("status", "title", "pk"),
    "-status": ("-status", "title", "pk"),
}


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


def _user_data(user):
    if user is None:
        return None

    return {
        "pk": user.pk,
        "username": "",
        "get_full_name": user.get_full_name(),
        "display_name": user.get_full_name() or "Nieuzupełnione dane",
        "is_active": user.is_active,
    }


def _empty_workload():
    return {"reading": 0, "completed": 0, "total": 0}


def _workloads_for_users(user_ids):
    user_ids = set(user_ids)
    if not user_ids:
        return {}

    rows = (
        ReviewAssignment.objects.filter(
            user_id__in=user_ids,
            review__old_reviews=False,
            review__is_hidden=False,
        )
        .order_by()
        .values("user_id")
        .annotate(
            reading_count=Count(
                "pk",
                filter=Q(
                    opinion__in=READING_OPINIONS,
                    review__status__in=OPEN_STATUSES,
                ),
            ),
            completed_count=Count(
                "pk",
                filter=~Q(opinion__in=READING_OPINIONS),
            ),
        )
    )

    result = {user_id: _empty_workload() for user_id in user_ids}

    for row in rows:
        reading = row["reading_count"]
        completed = row["completed_count"]
        result[row["user_id"]] = {
            "reading": reading,
            "completed": completed,
            "total": reading + completed,
        }

    return result


def reviewer_workload(user):
    """
    Zwraca bieżące zadania oraz oddane opinie użytkownika.

    Archiwalne recenzje nie są liczone. Nieoddana opinia dotycząca
    zamkniętej recenzji nie jest zadaniem oczekującym na wykonanie.
    """
    require_team_member(user)
    return _workloads_for_users({user.pk}).get(user.pk, _empty_workload())


def _leave_information_for_users(user_ids):
    if not user_ids:
        return {}

    now = timezone.now()
    today = timezone.localdate(now)

    vacations = (
        Vacation.objects.filter(
            person__user_id__in=user_ids,
            person__is_active=True,
        )
        .filter(Q(until_revoked=True) | Q(end_date__gt=now))
        .order_by("start_date", "pk")
        .values(
            "pk",
            "person__user_id",
            "start_date",
            "end_date",
            "until_revoked",
        )
    )

    result = {}

    for vacation in vacations:
        user_id = vacation["person__user_id"]

        # Wcześniejszy trwający urlop ma pierwszeństwo przed przyszłym.
        if user_id in result:
            continue

        is_active = vacation["start_date"] <= today
        result[user_id] = {
            "vacation": {
                "pk": vacation["pk"],
                "start_date": vacation["start_date"],
                "end_date": vacation["end_date"],
                "until_revoked": vacation["until_revoked"],
            },
            "is_active": is_active,
            "is_upcoming": not is_active,
        }

    return result


def _project_reviews(reviews, *, user, include_authors, allow_self_assignment):
    user_ids = {
        assignment.user_id
        for review in reviews
        for assignment in review.selector_assignments
        if assignment.user_id is not None
    }
    workloads = _workloads_for_users(user_ids)
    leave_information = _leave_information_for_users(user_ids)
    rows = []

    for review in reviews:
        assignments = review.selector_assignments
        own_assignment = next(
            (
                assignment
                for assignment in assignments
                if assignment.user_id == user.pk
            ),
            None,
        )
        locked = review.old_reviews or review.status not in OPEN_STATUSES
        opinions = []
        reviewer_workloads = []

        for assignment in assignments:
            reviewer = _user_data(assignment.user)
            reviewer_name = (
                assignment.reviewer_display_name
            )
            opinions.append(
                {
                    "pk": assignment.pk,
                    "position": assignment.position,
                    "slot": assignment.position,
                    "user": reviewer,
                    "reviewer_name": reviewer_name,
                    "opinion_value": assignment.opinion,
                    "opinion": assignment.get_opinion_display(),
                    "opinion_display": assignment.get_opinion_display(),
                    "get_opinion_display": assignment.get_opinion_display(),
                    "assigned_at": assignment.assigned_at,
                    "opinion_changed_at": assignment.opinion_changed_at,
                    "status_changed_at": assignment.opinion_changed_at,
                    "is_own": assignment.user_id == user.pk,
                }
            )

            if assignment.user_id is not None:
                reviewer_workloads.append(
                    {
                        "slot": assignment.position,
                        "user": reviewer,
                        "workload": workloads.get(
                            assignment.user_id,
                            _empty_workload(),
                        ),
                        "leave": leave_information.get(assignment.user_id),
                    }
                )

        assigned_count = len(assignments)
        completed_count = sum(
            assignment.opinion not in READING_OPINIONS
            for assignment in assignments
        )
        has_free_slot = assigned_count < MAX_REVIEWERS

        row = {
            "pk": review.pk,
            "title": review.title,
            "genre": review.genre,
            "length": review.length,
            "content_warnings": review.content_warnings,
            "status": review.status,
            "get_status_display": review.display_status,
            "created_at": review.created_at,
            "decision_at": review.decision_at,
            "old_reviews": review.old_reviews,
            "anthology_id": review.anthology_id,
            "anthology": (
                {
                    "pk": review.anthology.pk,
                    "title": review.anthology.title,
                }
                if review.anthology_id is not None
                else None
            ),
            "current_user_slot": (
                own_assignment.position if own_assignment else None
            ),
            "assigned_reviewer_count": assigned_count,
            "completed_reviewer_count": completed_count,
            "assignments": opinions,
            "reviewer_opinions": opinions,
            "reviewer_workloads": reviewer_workloads,
            "reviewers_have_free_slot": has_free_slot and not locked,
            "is_closed_for_assignments": locked,
            "is_locked": locked,
            "is_copied_to_text": review.copied_text_id is not None,
            "copied_text_id": review.copied_text_id,
            "can_self_assign_review": (
                allow_self_assignment
                and not locked
                and has_free_slot
                and own_assignment is None
            ),
            "can_unassign_review": own_assignment is not None and not locked,
            "all_reviews_finished": (
                assigned_count > 0 and completed_count == assigned_count
            ),
        }

        if include_authors:
            row.update(
                {
                    "authors": review.display_authors,
                    "author_id": review.author_id,
                    "author_first_name": review.author_first_name,
                    "author_last_name": review.author_last_name,
                    "email": review.email,
                    "phone_number": review.phone_number,
                    "author_notified_at": review.author_notified_at,
                    "notification_scheduled": bool(review.decision_at and review.decision_at > timezone.localdate()),
                }
            )

        rows.append(row)

    return rows


class _ReviewRows:
    """Stronicuje w bazie i zbiorczo przygotowuje dane wybranej strony."""

    def __init__(self, queryset, *, user, include_authors):
        self.queryset = queryset
        self.user = user
        self.include_authors = include_authors
        self.allow_self_assignment = can_self_assign_reviews(user)

    def count(self):
        return self.queryset.count()

    def __len__(self):
        return self.count()

    def _project(self, reviews):
        return _project_reviews(
            reviews,
            user=self.user,
            include_authors=self.include_authors,
            allow_self_assignment=self.allow_self_assignment,
        )

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self._project(list(self.queryset[key]))
        return self._project([self.queryset[key]])[0]

    def __iter__(self):
        # Iteracja poza paginatorem również nie pobiera całej bazy naraz.
        offset = 0
        batch_size = 100

        while True:
            batch = list(self.queryset[offset:offset + batch_size])
            if not batch:
                return

            yield from self._project(batch)
            offset += len(batch)


def review_list_context(*, user, params):
    require_team_member(user)
    include_authors = can_view_author_data(user)

    # Jeden znacznik archiwalności. Archiwum ogląda tylko superuser.
    old_reviews = (
        include_authors
        and params.get("old_reviews", "0").strip() == "1"
    )

    valid_statuses = {value for value, _label in Review.Status.choices}
    filters_applied = (
        params.get("filters_applied") == "1" or "status" in params
    )

    if filters_applied:
        selected_statuses = list(
            dict.fromkeys(
                value
                for value in params.getlist("status")
                if value in valid_statuses
            )
        )
    else:
        selected_statuses = [] if old_reviews else list(DEFAULT_STATUSES)

    assignment_state = params.get("assignment_state", "").strip()
    if assignment_state not in ASSIGNMENT_STATES:
        assignment_state = ""

    completed = params.get("completed", "").strip()
    if completed not in {str(n) for n in range(7)}:
        completed = ""

    anthology_id = _positive_id(params.get("anthology"))
    query = params.get("q", "").strip()

    queryset = Review.objects.visible_to(user).filter(old_reviews=old_reviews)
    if params.get("notification") == "pending":
        queryset = queryset.awaiting_notification()
        selected_statuses = []


    for term in query.split():
        condition = (
            Q(title__plcontains=term)
            | Q(anthology__title__plcontains=term)
        )

        if include_authors:
            condition |= (
                Q(author_first_name__plcontains=term)
                | Q(author_last_name__plcontains=term)
                | Q(email__plcontains=term)
                | Q(author__first_name__plcontains=term)
                | Q(author__last_name__plcontains=term)
                | Q(author__pseudonym__plcontains=term)
                | Q(coauthors__first_name__plcontains=term)
                | Q(coauthors__last_name__plcontains=term)
                | Q(coauthors__pseudonym__plcontains=term)
                | Q(author__email__plcontains=term)
            )

        queryset = queryset.filter(condition)

    # Liczymy rekordy przydziałów, również po usunięciu konta.
    # Usunięcie użytkownika nie zwalnia automatycznie zajętej pozycji.
    queryset = queryset.annotate(
        assigned_count=Count("assignments", distinct=True),
        completed_count=Count(
            "assignments",
            filter=~Q(assignments__opinion__in=READING_OPINIONS),
            distinct=True,
        ),
    )

    if completed != "":
        queryset = queryset.filter(completed_count=int(completed))

    if assignment_state == "none":
        queryset = queryset.filter(assigned_count=0)
    elif assignment_state == "one":
        queryset = queryset.filter(assigned_count=1)
    elif assignment_state == "full":
        queryset = queryset.filter(assigned_count__gte=MAX_REVIEWERS)
    elif assignment_state in {"all_finished", "awaiting_decision"}:
        queryset = queryset.filter(
            assigned_count__gt=0,
            completed_count=F("assigned_count"),
        )
        if assignment_state == "awaiting_decision":
            queryset = queryset.filter(status=Review.Status.IN_REVIEW)

    queryset, facets = facet_queryset(queryset, {
        'status': ('status', selected_statuses),
        'anthology': ('anthology_id', [anthology_id] if anthology_id else []),
    })
    sort = params.get("sort", "newest")
    if sort not in SORT_FIELDS:
        sort = "newest"

    queryset = (
        queryset.select_related("anthology", "author").prefetch_related("coauthors")
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=(
                    ReviewAssignment.objects.select_related("user", "historical_person")
                    .order_by("position", "pk")
                ),
                to_attr="selector_assignments",
            )
        )
        .order_by(*SORT_FIELDS[sort])
    )

    return {
        "reviews": _ReviewRows(
            queryset,
            user=user,
            include_authors=include_authors,
        ),
        "anthologies": list(
            Anthology.objects.filter(pk__in=facets["anthology"]).order_by("title", "pk").values("pk", "title")
        ),
        "selected_statuses": selected_statuses,
        "selected_assignment_state": assignment_state,
        "selected_completed": completed,
        "selected_anthology_id": anthology_id,
        "status_choices": [(v, label) for v, label in Review.Status.choices if v in facets["status"]],
        "query": query,
        "sort": sort,
        "old_reviews": old_reviews,
        "selected_old_reviews": old_reviews,
        "max_reviewers": MAX_REVIEWERS,
        "filters_are_default": (
            not old_reviews
            and set(selected_statuses) == set(DEFAULT_STATUSES)
            and not completed
            and not assignment_state
            and anthology_id is None
            and not query
            and sort == "newest"
        ),
    }
