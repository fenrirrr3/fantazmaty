from core.filtering import facet_queryset
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import router, transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from authors.models import Author
from core.pagination import paginate_queryset
from core.permissions import get_active_person_profile, is_coordinator, can_view_illustrations
from texts.models import Anthology

from .forms import CoverProposalForm
from .models import CoverProposal, Illustration


ILLUSTRATION_SORT_FIELDS = {
    "anthology": (
        "text__anthology__title",
        "text__title",
    ),
    "title": (
        "text__title",
        "text__anthology__title",
    ),
    "illustrator": (
        "illustrator__last_name",
        "illustrator__first_name",
    ),
    "status": ("status",),
    "assigned_at": ("assigned_at",),
}


def has_coordinator_access(user):
    return is_coordinator(user)


def _ensure_team_access(user):
    if not (
        user.is_authenticated
        and user.is_active
        and (
            user.is_superuser
            or get_active_person_profile(user) is not None
        )
    ):
        raise PermissionDenied(
            "Dostęp wymaga aktywnego konta i przynależności do zespołu."
        )


def _positive_id(value):
    value = (value or "").strip()

    if (
        not value
        or len(value) > 19
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None

    parsed = int(value)

    if not 0 < parsed <= 9223372036854775807:
        return None

    return parsed


def _illustration_ordering(request):
    selected_sort = request.GET.get("sort", "anthology").strip()
    descending = selected_sort.startswith("-")
    field_name = selected_sort[1:] if descending else selected_sort

    if field_name not in ILLUSTRATION_SORT_FIELDS:
        selected_sort = "anthology"
        field_name = "anthology"
        descending = False

    prefix = "-" if descending else ""
    ordering = [
        f"{prefix}{field}"
        for field in ILLUSTRATION_SORT_FIELDS[field_name]
    ]
    ordering.append(f"{prefix}pk")

    return selected_sort, ordering


@never_cache
@login_required
@require_GET
def illustration_list(request):
    _ensure_team_access(request.user)
    if not can_view_illustrations(request.user):
        raise PermissionDenied("Ilustracje są dostępne dla koordynatorów i ilustratorów.")

    selected_anthology_id = _positive_id(
        request.GET.get("anthology", "")
    )
    selected_statuses = [v for v in request.GET.getlist("status") if v in Illustration.Status.values]
    selected_status = selected_statuses[-1] if selected_statuses else ""

    if selected_status not in Illustration.Status.values:
        selected_status = ""

    selected_sort, ordering = _illustration_ordering(request)
    can_view_authors = request.user.is_superuser

    # GET nie tworzy ani nie aktualizuje rekordów.
    # Ilustracje powstają przez istniejące sygnały przy zmianach danych.
    illustrations = (
        Illustration.objects.filter(
            text__anthology__status=Anthology.Status.UNPUBLISHED,
            text__anthology__has_illustrations=True,
        )
        .select_related(
            "text",
            "text__anthology",
            "illustrator",
        )
        .order_by(*ordering)
    )

    illustrations, facets = facet_queryset(illustrations, {
        'status': ('status', selected_statuses),
        'anthology': ('text__anthology_id', [selected_anthology_id] if selected_anthology_id else []),
    })

    if can_view_authors:
        authors = Author.objects.only(
            "pk",
            "first_name",
            "last_name",
            "pseudonym",
        ).order_by("last_name", "first_name", "pk")
    else:
        # Zapobiega ujawnieniu autorów również przez dotychczasowe
        # odwołania illustration.text.authors.all w szablonie.
        authors = Author.objects.none()

    illustrations = illustrations.prefetch_related(
        Prefetch("text__authors", queryset=authors)
    )

    anthologies = (
        Anthology.objects.filter(
            status=Anthology.Status.UNPUBLISHED,
            has_illustrations=True,
        )
        .only("pk", "title")
        .order_by("title", "pk")
    )

    page_obj = paginate_queryset(request, illustrations)

    return render(
        request,
        "core/illustration_list.html",
        {
            "illustrations": page_obj,
            "page_obj": page_obj,
            "anthologies": anthologies.filter(pk__in=facets["anthology"]),
            "status_choices": [(v, label) for v, label in Illustration.Status.choices if v in facets["status"]],
            "selected_anthology_id": selected_anthology_id,
            "selected_status": selected_status, "selected_statuses": selected_statuses,
            "selected_sort": selected_sort,
            "can_view_authors": can_view_authors,
        },
    )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def cover_proposal_list(request):
    _ensure_team_access(request.user)

    form = CoverProposalForm(
        request.POST if request.method == "POST" else None
    )

    if request.method == "POST" and form.is_valid():
        proposal = form.save(commit=False)

        # Wartości kontrolowane przez serwer, niezależnie od POST.
        proposal.submitted_by = request.user
        proposal.status = CoverProposal.Status.PENDING
        proposal.save()

        messages.success(
            request,
            "Propozycja ilustracji została zgłoszona.",
        )
        return redirect("illustrations:cover_proposal_list")

    proposals = (
        CoverProposal.objects.select_related("submitted_by")
        .order_by("-submitted_at", "-pk")
    )
    page_obj = paginate_queryset(request, proposals)

    return render(
        request,
        "core/cover_proposal_list.html",
        {
            "form": form,
            "proposals": page_obj,
            "page_obj": page_obj,
            "status_choices": CoverProposal.Status.choices,
            "can_manage_proposals": has_coordinator_access(request.user),
        },
    )


@never_cache
@login_required
@require_POST
def update_cover_proposal_status(request, proposal_id):
    _ensure_team_access(request.user)

    if not has_coordinator_access(request.user):
        raise PermissionDenied(
            "Tylko koordynator może zmieniać status propozycji."
        )

    new_status = request.POST.get("status", "").strip()

    if new_status not in CoverProposal.Status.values:
        messages.error(request, "Wybrano nieprawidłowy status.")
        return redirect("illustrations:cover_proposal_list")

    using = router.db_for_write(CoverProposal)

    with transaction.atomic(using=using):
        proposal = get_object_or_404(
            CoverProposal.objects.using(using).select_for_update(),
            pk=proposal_id,
        )

        if proposal.status == new_status:
            changed = False
        else:
            proposal.status = new_status
            proposal.save(
                using=using,
                update_fields=["status"],
            )
            changed = True

        status_label = proposal.get_status_display()

    if changed:
        messages.success(
            request,
            f"Status propozycji zmieniono na „{status_label}”.",
        )
    else:
        messages.info(
            request,
            "Propozycja ma już wybrany status.",
        )

    return redirect("illustrations:cover_proposal_list")
