"""Explicit, privacy-preserving projections for the review detail page."""
from texts.models import ReviewAssignment, Reviewers


def review_template_data(review, *, include_author):
    """
    Szablon otrzymuje jawnie wybrane wartości zamiast obiektu ORM.

    Dzięki temu nie może dotrzeć do autora przez relacje Review,
    copied_text, przydziały lub formularz powiązany z instancją.
    """
    anthology = review.anthology
    data = {
        "pk": review.pk,
        "id": review.pk,
        "title": review.title,
        "genre": review.genre,
        "length": review.length,
        "content_warnings": review.content_warnings,
        "status": review.status,
        "get_status_display": review.display_status,
        "created_at": review.created_at,
        "decision_at": review.decision_at,
        "old_reviews": review.old_reviews,
        "is_hidden": review.is_hidden,
        "copied_text_id": review.copied_text_id,
        "anthology_id": review.anthology_id,
        "anthology": (
            {
                "pk": anthology.pk,
                "id": anthology.pk,
                "title": anthology.title,
            }
            if anthology is not None
            else None
        ),
    }

    if include_author:
        author = review.author
        data.update(
            {
                "author_first_name": review.author_first_name,
                "author_last_name": review.author_last_name,
                "email": review.email,
                "phone_number": review.phone_number,
                "author_notified_at": review.author_notified_at,
                "authors": review.display_authors,
                "author_id": review.author_id,
                "author": (
                    {
                        "pk": author.pk,
                        "id": author.pk,
                        "first_name": author.first_name,
                        "last_name": author.last_name,
                        "pseudonym": author.pseudonym,
                        "email": author.email,
                        "has_contract": author.has_contract,
                    }
                    if author is not None
                    else None
                ),
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

    opinions = []

    for assignment in assignments:
        reviewer = assignment.user
        reviewer_name = (
            assignment.reviewer_display_name
        )
        opinions.append(
            {
                "pk": assignment.pk,
                "position": assignment.position,
                "slot": assignment.position,
                "reviewer_name": reviewer_name,
                "user_name": reviewer_name,
                "opinion_value": assignment.opinion,
                "opinion": assignment.get_opinion_display(),
                "opinion_display": assignment.get_opinion_display(),
                "notes": assignment.notes,
                "assigned_at": assignment.assigned_at,
                "status_changed_at": assignment.opinion_changed_at,
                "opinion_changed_at": assignment.opinion_changed_at,
                "is_own": assignment.user_id == user.pk,
            }
        )

    opinion_summary = {
        value: sum(item["opinion_value"] == value for item in opinions)
        for value, _label in Reviewers.Opinion.choices
    }
    opinion_summary["assigned"] = len(assignments)
    opinion_summary["completed"] = sum(
        assignment.opinion not in {"", Reviewers.Opinion.READING}
        for assignment in assignments
    )

    return assignments, own_assignment, opinions, opinion_summary
