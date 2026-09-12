from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.forms import GlobalSearchForm
from core.permissions import (
    can_view_author_data,
    can_view_reports,
    team_member_required,
)
from core.selectors.people import user_leave_information
from core.selectors.reviews import reviewer_workload
from core.selectors.texts import user_workflow_summary
from texts.models import Review


@never_cache
@login_required
@require_GET
@team_member_required
def home(request):
    today = timezone.localdate()
    from core.permissions import is_reviewer_only
    from texts.models import ReviewAssignment
    reviewer_only = is_reviewer_only(request.user)
    own_reviews = ReviewAssignment.objects.filter(user=request.user, review__is_hidden=False, review__old_reviews=False, review__status__in=("new", "in_review")).select_related("review__anthology")
    review_reading = own_reviews.filter(opinion="reading")
    review_reserved = own_reviews.filter(opinion="")

    # Selektor uwzględnia aktualny przebieg, rolę przypisaną do danego
    # etapu oraz datę rozpoczęcia. Pomija teksty gotowe i wycofane.
    workflow_summary = user_workflow_summary(
        request.user,
        today=today,
    )

    return render(
        request,
        "core/home.html",
        {
            "now": timezone.localtime(),
            "reviewer_only": reviewer_only,
            "review_reading": review_reading,
            "review_reserved": review_reserved,
            "active_stages": workflow_summary["active_stages"],
            "reserved_assignments": workflow_summary["reserved_assignments"],
            "active_stage_count": review_reading.count() if reviewer_only else workflow_summary["active_stage_count"],
            "reserved_assignment_count": (
                review_reserved.count() if reviewer_only else workflow_summary["reserved_assignment_count"]
            ),
            "reviewer_load": reviewer_workload(request.user),
            "leave_information": user_leave_information(request.user),
            "search_form": GlobalSearchForm(user=request.user),
            "can_view_authors": can_view_author_data(request.user),
            "can_view_reports": can_view_reports(request.user),
            "today": today,
            "pending_notifications": (
                Review.objects.awaiting_notification().order_by("decision_at", "pk")
                if request.user.is_superuser else Review.objects.none()
            ),
        },
    )


@never_cache
@login_required
@require_GET
@team_member_required
def audiobooks(request):
    return render(
        request,
        "core/audiobooks.html",
    )
