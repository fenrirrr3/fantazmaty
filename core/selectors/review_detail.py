"""Explicit, privacy-preserving projections for the review detail page."""
from texts.models import ReviewAssignment, Reviewers


def review_template_data(review, *, include_author):
    """
    Szablon otrzymuje jawnie wybrane wartości zamiast obiektu ORM.

    Dzięki temu nie może dotrzeć do autora przez relacje Review,
    copied_text, przydziały lub formularz powiązany z instancją.
    """
    data = {
        "pk": review.pk,
        "title": review.title,
        "genre": review.genre,
        "length": review.length,
        "content_warnings": review.content_warnings,
        "status": review.status,
        "get_status_display": review.display_status,
        "created_at": review.created_at,
        "decision_at": review.decision_at,
        "is_archived": review.is_archived,
        "is_hidden": review.is_hidden,
        "copied_text_id": review.copied_text_id,
        "anthology": {"title": review.anthology.title} if review.anthology_id is not None else None,
    }

    if include_author:
        data.update(
            {
                "author_display_name": review.author_display_name,
                "email": review.email,
                "phone_number": review.phone_number,
                "file_url": review.file_url,
                "authors": review.display_authors,
            }
        )

    return data


def review_assignment_data(review, user):
    assignments = list(
        ReviewAssignment.objects.filter(review_id=review.pk)
        .select_related("user", "historical_person")
        .order_by("position", "pk")
    )
    own_assignment = next(
        (
            assignment
            for assignment in assignments
            if assignment.user_id == user.pk
        ),
        None,
    )

    # Opinie pozostałych recenzentów – tylko pola czytane przez szablon.
    other_opinions = [
        {
            "pk": assignment.pk,
            "position": assignment.position,
            "reviewer_name": assignment.reviewer_display_name,
            "opinion_value": assignment.opinion,
            "opinion_display": assignment.get_opinion_display(),
            "notes": assignment.notes,
            "assigned_at": assignment.assigned_at,
            "opinion_changed_at": assignment.opinion_changed_at,
        }
        for assignment in assignments
        if assignment.user_id != user.pk
    ]

    opinion_summary = {
        "assigned": len(assignments),
        "completed": sum(
            assignment.opinion not in {"", Reviewers.Opinion.READING}
            for assignment in assignments
        ),
    }

    return assignments, own_assignment, other_opinions, opinion_summary
