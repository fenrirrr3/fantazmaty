from core.filtering import facet_queryset
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from core.models import AnthologyCorrection
from core.pagination import paginate_items
from core.permissions import team_member_required, superuser_required
from texts.models import Anthology, Review, Text
from core.correction_forms import CorrectionForm
import uuid


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def corrections(request):
    form = CorrectionForm(request.POST if request.method == "POST" else None)
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.submitted_by = request.user
        key = form.cleaned_data.get("submission_token")
        with transaction.atomic():
            selected_text = get_object_or_404(Text.objects.select_for_update(), pk=item.text_id) if item.text_id else None
            if selected_text and selected_text.anthology_id != item.anthology_id:
                form.add_error("text", "Opowiadanie zmieniło antologię. Wybierz je ponownie.")
            else:
                item.story_title = selected_text.title if selected_text else "Inne miejsce"
                if key:
                    values = {name: getattr(item, name) for name in ("anthology_id", "text_id", "story_title", "fragment", "problem", "suggestion", "submitted_by_id")}
                    item, created = AnthologyCorrection.objects.get_or_create(submission_key=key, defaults=values)
                    if not created and any(getattr(item, name) != value for name, value in values.items()):
                        return JsonResponse({"errors": {"__all__": ["Identyfikator zapisu był już użyty dla innych danych. Odśwież formularz."]}}, status=409)
                else:
                    item.save()
        if not form.errors:
            if ajax:
                return JsonResponse({"ok": True, "id": item.pk, "next_token": str(uuid.uuid4()),
                    "message": "Dodano uwagę. Możesz wpisać kolejną.", "list_url": reverse("core:anthology_corrections")})
            messages.success(request, "Zapisano uwagę do antologii.")
            return redirect("core:anthology_corrections")
    if request.method == "POST" and ajax:
        return JsonResponse({"errors": {name: list(errors) for name, errors in form.errors.items()}}, status=400)
    items = AnthologyCorrection.objects.select_related("anthology", "submitted_by")
    query = request.GET.get("q", "").strip()[:255]
    statuses = [v for v in request.GET.getlist("status") if v in AnthologyCorrection.Status.values]
    status = statuses[-1] if statuses else ""
    anthology = request.GET.get("anthology", "")
    if query:
        items = items.filter(Q(story_title__plcontains=query) | Q(problem__plcontains=query))
    anthology_ids = [int(anthology)] if anthology.isdecimal() and len(anthology) < 19 else []
    items, facets = facet_queryset(items, {
        'status': ('status', statuses), 'anthology': ('anthology_id', anthology_ids),
    })
    page = paginate_items(request, items)
    return render(request, "core/anthology_corrections.html", {"form": form, "page_obj": page,
        "corrections": page, "statuses": AnthologyCorrection.Status.choices,
        "filter_statuses": [(v, label) for v, label in AnthologyCorrection.Status.choices if v in facets["status"]],
        "anthologies": Anthology.objects.filter(pk__in=facets["anthology"]).order_by("title"), "query": query,
        "selected_status": status, "selected_statuses": statuses, "selected_anthology": anthology}, status=400 if request.method == "POST" else 200)


@never_cache
@login_required
@require_GET
@team_member_required
def correction_texts(request):
    anthology = request.GET.get("anthology", "")
    if not anthology.isdecimal() or len(anthology) > 18:
        return JsonResponse({"texts": []})
    texts = Text.objects.filter(anthology_id=anthology).order_by("title", "pk").values("id", "title")
    return JsonResponse({"texts": list(texts)})


@never_cache
@login_required
@require_POST
@superuser_required
@transaction.atomic
def correction_status(request):
    ids = request.POST.getlist("selected")
    status = request.POST.get("status")
    if not ids or len(ids) > 1000 or any(not v.isdecimal() or len(v) > 18 for v in ids) or status not in AnthologyCorrection.Status.values:
        messages.error(request, "Zaznacz uwagi i wybierz poprawny status.")
        return redirect("core:anthology_corrections")
    rows = list(AnthologyCorrection.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if {row.pk for row in rows} != {int(value) for value in ids}:
        return render(request, "core/edit_conflict.html", status=409)
    for row in rows:
        # All selected rows are validated before any write.
        if request.POST.get(f"version_{row.pk}") != row.updated_at.isoformat():
            return render(request, "core/edit_conflict.html", status=409)
    for row in rows:
        row.status = status
        row.save(update_fields=("status", "updated_at"))
    messages.success(request, f"Zmieniono status {len(rows)} uwag.")
    return redirect("core:anthology_corrections")


@never_cache
@login_required
@require_GET
@superuser_required
def notification_queue(request, scheduled=False):
    reviews = Review.objects.filter(is_hidden=True, status=Review.Status.REJECTED,
        decision_at__gt=timezone.localdate(), old_reviews=False) if scheduled else Review.objects.awaiting_notification()
    query = request.GET.get("q", "").strip()[:255]
    if query:
        reviews = reviews.filter(Q(title__plcontains=query) | Q(email__plcontains=query))
    page = paginate_items(request, reviews.select_related("anthology").order_by("decision_at", "pk"))
    return render(request, "core/notification_queue.html", {"page_obj": page, "reviews": page,
        "scheduled": scheduled, "query": query})


@never_cache
@login_required
@require_POST
@superuser_required
@transaction.atomic
def release_hidden_review(request, review_id):
    review = get_object_or_404(Review.objects.select_for_update(), pk=review_id, is_hidden=True)
    if request.POST.get("confirm") != "yes":
        messages.error(request, "Potwierdź przywrócenie zgłoszenia do recenzji.")
    elif review.old_reviews or review.copied_text_id:
        messages.error(request, "Archiwalnego lub skopiowanego zgłoszenia nie można przywrócić w ten sposób.")
    else:
        review.is_hidden = False
        review.status = Review.Status.NEW
        review.decision_at = None
        review.author_notified_at = None
        review.save(update_fields=("is_hidden", "status", "decision_at", "author_notified_at"))
        messages.success(request, "Przywrócono widoczność zgłoszenia. Oznaczenie autora na czarnej liście możesz osobno poprawić w panelu.")
    return redirect("core:assigned_review_detail", review_id=review.pk)
