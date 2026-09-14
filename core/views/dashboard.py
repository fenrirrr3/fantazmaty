from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.core.paginator import Paginator
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
    reviewer_only = is_reviewer_only(request.user)
    workflow_summary = {"active_stages": [], "reserved_assignments": [],
                        "active_stage_count": 0, "reserved_assignment_count": 0}
    review_reading, review_reserved = [], []
    if reviewer_only:
        reading, reserved = _review_tasks(request.user)
        workflow_summary["active_stage_count"] = reading.count()
        workflow_summary["reserved_assignment_count"] = reserved.count()
        review_reading, review_reserved = list(reading[:6]), list(reserved[:6])
    else:
        workflow_summary = user_workflow_summary(request.user, today=today, limit=6)

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
            "active_stage_count": workflow_summary["active_stage_count"],
            "reserved_assignment_count": (
                workflow_summary["reserved_assignment_count"]
            ),
            "reviewer_load": reviewer_workload(request.user),
            "leave_information": user_leave_information(request.user),
            "search_form": GlobalSearchForm(user=request.user),
            "can_view_authors": can_view_author_data(request.user),
            "can_view_reports": can_view_reports(request.user),
            "today": today,
            "pending_notification_count": Review.objects.awaiting_notification().count() if request.user.is_superuser else 0,
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


def _review_tasks(user):
    from texts.models import ReviewAssignment
    rows = ReviewAssignment.objects.filter(user=user, review__is_hidden=False,
        review__old_reviews=False, review__status__in=("new", "in_review")).select_related(
            "review__anthology").order_by("assigned_at", "pk")
    return rows.filter(opinion="reading"), rows.filter(opinion="")


@never_cache
@login_required
@require_GET
@team_member_required
def dashboard_tasks(request):
    from core.permissions import is_reviewer_only
    from workflow.read_queries import dashboard_querysets
    kind = request.GET.get("kind", "active")
    if kind not in {"active", "reserved"}:
        return JsonResponse({"error": "Nieprawidłowy rodzaj zadań."}, status=400)
    reviewer = is_reviewer_only(request.user)
    if reviewer:
        active, reserved = _review_tasks(request.user)
    else:
        active, reserved = dashboard_querysets(request.user, timezone.localdate())
        active = active.select_related("text__anthology")
        reserved = reserved.select_related("text__anthology")
    rows = active if kind == "active" else reserved
    page = Paginator(rows, 6).get_page(request.GET.get("page", "1"))
    template = "core/includes/dashboard_review_items.html" if reviewer else "core/includes/dashboard_work_items.html"
    html = render_to_string(template, {"rows": page, "kind": kind}, request=request)
    next_url = (reverse("core:dashboard_tasks") + f"?kind={kind}&page={page.next_page_number()}") if page.has_next() else None
    return JsonResponse({"html": html, "next_url": next_url})
