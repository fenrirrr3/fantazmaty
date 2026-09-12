from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.pagination import paginate_items
from core.permissions import can_view_author_data, team_member_required
from texts.models import Anthology, AnthologyTask


@never_cache
@login_required
@require_GET
@team_member_required
def anthology_list(request):
    production_tasks = (
        AnthologyTask.objects
        .select_related("assigned_to")
        .order_by("task_type", "pk")
    )

    anthologies = (
        Anthology.objects
        .prefetch_related(
            Prefetch(
                "production_tasks",
                queryset=production_tasks,
            )
        )
        .order_by("title", "pk")
    )

    # Stronicowanie ogranicza również pobieranie zadań produkcyjnych
    # do antologii widocznych na bieżącej stronie.
    page_obj = paginate_items(request, anthologies)

    return render(
        request,
        "core/anthology_list.html",
        {
            "anthologies": page_obj,
            "page_obj": page_obj,
            "can_view_authors": can_view_author_data(request.user),
        },
    )