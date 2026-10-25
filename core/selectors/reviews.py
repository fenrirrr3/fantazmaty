from core.public_authors import review_name_matches
from core.translation_scope import frontend_scope
from core.filtering import facet_queryset, positive_id
from functools import partial

from django.db.models import Count, Prefetch, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.permissions import (
    can_manage_reviews,
    can_self_assign_reviews,
    can_view_author_data,
    can_view_review_archive,
    can_view_archived_review_authors,
    require_team_member,
)
from core.services.reviews import OPEN_STATUSES
from texts.models import Anthology, Review, ReviewAssignment, Reviewers


READING_OPINIONS = ("", Reviewers.Opinion.READING)
MAX_REVIEWERS = ReviewAssignment.MAX_REVIEWERS


def reviewer_workload(user):
    """
    Zwraca bieżące zadania oraz oddane opinie użytkownika.

    Oddane archiwalne recenzje zwiększają tylko dorobek. Nieoddana opinia dotycząca
    zamkniętej recenzji nie jest zadaniem oczekującym na wykonanie.
    """
    require_team_member(user)
    return (
        frontend_scope(ReviewAssignment.objects).for_statistics()
        .annotate(reviewer_user_id=Coalesce("user_id", "historical_person__user_id"))
        .filter(reviewer_user_id=user.pk)
        .aggregate(
            reading=Count("pk", filter=Q(opinion__in=READING_OPINIONS, review__old_reviews=False,
                                         review__status__in=OPEN_STATUSES)),
            completed=Count("pk", filter=~Q(opinion__in=READING_OPINIONS)),
        )
    )


def _project_reviews(reviews, *, user, include_authors, allow_self_assignment):
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
        # Tylko pola czytane przez szablon listy.
        opinions = [
            {
                "reviewer_name": assignment.reviewer_display_name,
                "opinion_value": assignment.opinion,
                "opinion_display": assignment.get_opinion_display(),
                "opinion_changed_at": assignment.opinion_changed_at,
            }
            for assignment in assignments
        ]

        assigned_count = len(assignments)
        completed_count = sum(
            assignment.opinion not in READING_OPINIONS
            for assignment in assignments
        )
        has_free_slot = assigned_count < MAX_REVIEWERS
        reading_blocked = not locked and review.reading_block_active([item.opinion for item in assignments])

        row = {
            "pk": review.pk,
            "title": review.title,
            "genre": review.genre,
            "length": review.length,
            "status": review.status,
            "get_status_display": review.display_status,
            "created_at": review.created_at,
            "decision_at": review.decision_at,
            "is_archived": review.is_archived,
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
            "is_copied_to_text": review.copied_text_id is not None,
            "can_self_assign_review": (
                allow_self_assignment
                and not locked
                and has_free_slot
                and own_assignment is None
                and not reading_blocked
            ),
            "reading_blocked": reading_blocked,
        }

        if include_authors:
            row.update(
                {
                    "authors": review.display_authors,
                    "author_display_name": review.author_display_name,
                    "email": review.email,
                    "author_notified_at": review.author_notified_at,
                    "notification_scheduled": bool(review.decision_at and review.decision_at > timezone.localdate()),
                }
            )

        rows.append(row)

    return rows


class _ReviewRows:
    """Stronicuje w bazie i zbiorczo przygotowuje dane wybranej strony.

    Bez atrybutu `projector`: sortowanie po kolumnie wyliczanej (Autor) czyta pole z rekordu,
    a wybraną stronę projektuje zbiorczo (`batch_projector`).
    """

    def __init__(self, queryset, batch_projector):
        self.queryset = queryset
        self.batch_projector = batch_projector

    def count(self):
        return self.queryset.count()

    def __len__(self):
        return self.count()

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self.batch_projector(list(self.queryset[key]))
        return self.batch_projector([self.queryset[key]])[0]

    def __iter__(self):
        # Iteracja poza paginatorem również nie pobiera całej bazy naraz.
        offset = 0
        while batch := list(self.queryset[offset:offset + 100]):
            yield from self.batch_projector(batch)
            offset += len(batch)


def review_list_context(*, user, params):
    require_team_member(user)
    requested_statuses = set(params.getlist("status"))
    old_reviews = can_view_review_archive(user) and (params.get("old_reviews", "0").strip() == "1"
        or bool(requested_statuses and requested_statuses <= {"accepted", "rejected"}))
    include_authors = can_view_author_data(user) or (old_reviews and can_view_archived_review_authors(user))

    valid_statuses = {"accepted", "rejected"} if old_reviews else OPEN_STATUSES
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
        selected_statuses = []

    completed = params.get("completed", "").strip()
    if completed not in {str(n) for n in range(7)}:
        completed = ""

    anthology_id = positive_id(params.get("anthology"))
    query = params.get("q", "").strip()

    queryset = frontend_scope(Review.objects, include_abandoned=old_reviews).visible_to(user)
    queryset = queryset.archived() if old_reviews else queryset.current()

    if not old_reviews and not can_manage_reviews(user):
        # Zablokowane oceną „Nie doczytałem” znika z listy do wzięcia;
        # przypisani recenzenci nadal widzą swoje zgłoszenie.
        queryset = queryset.exclude(pk__in=Review.objects.reading_blocked().exclude(assignments__user=user).values("pk"))
    queryset = _filter_review_work(queryset, query, include_authors, completed)

    queryset, facets = facet_queryset(queryset, {
        'status': ('status', selected_statuses),
        'anthology': ('anthology_id', [anthology_id] if anthology_id else []),
    })
    if include_authors:
        queryset = queryset.select_related("author").prefetch_related("coauthors")

    queryset = (
        queryset.select_related("anthology")
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=(
                    frontend_scope(ReviewAssignment.objects, include_abandoned=old_reviews).select_related("user", "historical_person")
                    .order_by("position", "pk")
                ),
                to_attr="selector_assignments",
            )
        )
        # Kolejność z nagłówka tabeli ustawia paginate_items (core.table_sorting); domyślnie od najnowszych.
        .order_by("-pk")
    )
    project = partial(_project_reviews, user=user, include_authors=include_authors,
                      allow_self_assignment=can_self_assign_reviews(user))

    return {
        "reviews": _ReviewRows(queryset, project),
        "anthologies": list(
            frontend_scope(Anthology.objects, include_abandoned=old_reviews).filter(pk__in=facets["anthology"]).order_by("title", "pk").values("pk", "title")
        ),
        "selected_statuses": selected_statuses,
        "selected_completed": completed,
        "selected_anthology_id": anthology_id,
        "status_choices": [(v, label) for v, label in Review.Status.choices if v in valid_statuses],
        "query": query,
        "old_reviews": old_reviews,
        "max_reviewers": MAX_REVIEWERS,
    }


def _filter_review_work(queryset, query, include_authors, completed):
    for term in query.split():
        condition = (
            Q(title__plcontains=term)
            | Q(anthology__title__plcontains=term)
        )

        if include_authors:
            condition |= (
                review_name_matches(term)
            )

        queryset = queryset.filter(condition)

    # Liczymy rekordy przydziałów, również po usunięciu konta (sortowanie „Recenzenci i opinie” czyta tę adnotację).
    queryset = queryset.annotate(
        completed_count=Count(
            "assignments",
            filter=~Q(assignments__opinion__in=READING_OPINIONS),
            distinct=True,
        ),
    )

    if completed != "":
        queryset = queryset.filter(completed_count=int(completed))

    return queryset
