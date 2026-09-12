from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from authors.models import Author, AuthorNote
from core.forms import AuthorNoteForm
from core.pagination import paginate_items
from core.permissions import superuser_required
from texts.models import Anthology, Review, Text


MAX_DATABASE_ID = 9_223_372_036_854_775_807

# Pole is_blacklisted pozostaje dostępne wyłącznie w panelu admina.
AUTHOR_DISPLAY_FIELDS = (
    "pk",
    "first_name",
    "last_name",
    "pseudonym",
    "email",
    "has_contract",
    "contact",
)


def _positive_id(value):
    value = value.strip()

    if (
        not value
        or len(value) > 19
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None

    result = int(value)
    return result if 1 <= result <= MAX_DATABASE_ID else None


def _yes_no_filter(value):
    value = value.strip()
    return value if value in {"yes", "no"} else ""


def _author_display_data(author):
    return {
        field: getattr(author, field)
        for field in AUTHOR_DISPLAY_FIELDS
    }


def _render_author_detail(request, author, *, form=None, status=200):
    author_data = _author_display_data(author)

    notes = (
        AuthorNote.objects.filter(author_id=author.pk)
        .select_related("created_by")
        .order_by("-created_at", "-pk")
    )
    note_history = []

    for note in notes:
        creator = note.created_by
        note_history.append(
            {
                "pk": note.pk,
                "content": note.content,
                "created_at": note.created_at,
                "created_by": (
                    {
                        "get_full_name": creator.get_full_name(),
                        "username": "",
                    }
                    if creator is not None
                    else None
                ),
            }
        )

    author_data["note_history"] = note_history

    text_queryset = Text.objects.filter(authors__pk=author.pk)
    texts = []

    for item in text_queryset.order_by("title", "pk").values(
        "pk",
        "title",
        "length",
        "anthology_id",
        "anthology__title",
        "source_review__genre",
    ):
        texts.append(
            {
                "pk": item["pk"],
                "title": item["title"],
                "length": item["length"],
                "anthology": (
                    {
                        "pk": item["anthology_id"],
                        "title": item["anthology__title"],
                    }
                    if item["anthology_id"] is not None
                    else None
                ),
                "source_review": {
                    "genre": item["source_review__genre"] or "",
                },
            }
        )

    # Archiwalne recenzje nie wpływają na żaden licznik zgłoszeń.
    submissions = Review.objects.filter(
        author_id=author.pk,
        old_reviews=False,
    )
    author_summary = submissions.aggregate(
        submissions=Count("pk"),
        accepted=Count(
            "pk",
            filter=Q(status=Review.Status.ACCEPTED),
        ),
        rejected=Count(
            "pk",
            filter=Q(status=Review.Status.REJECTED),
        ),
    )
    author_summary["texts"] = len(texts)
    author_summary["anthologies"] = Anthology.objects.filter(
        Q(
            pk__in=text_queryset.order_by().values("anthology_id"),
        )
        | Q(
            pk__in=submissions.order_by().values("anthology_id"),
        )
    ).count()

    return render(
        request,
        "core/author_detail.html",
        {
            "author": author_data,
            "texts": texts,
            "author_summary": author_summary,
            "can_view_authors": True,
            "can_manage_author_notes": True,
            "author_note_form": form if form is not None else AuthorNoteForm(),
        },
        status=status,
    )


@never_cache
@login_required
@require_GET
@superuser_required
def author_list(request):
    query = request.GET.get("q", "").strip()
    contract_filter = _yes_no_filter(request.GET.get("contract", ""))
    contact_filter = _yes_no_filter(request.GET.get("contact", ""))
    selected_anthology_id = _positive_id(
        request.GET.get("anthology", "")
    )

    authors = Author.objects.all()

    # Każdy człon zapytania musi pasować do przynajmniej jednego pola.
    # Obsługuje to również wyszukiwanie po pełnym imieniu i nazwisku.
    for term in query.split():
        authors = authors.filter(
            Q(first_name__plcontains=term)
            | Q(last_name__plcontains=term)
            | Q(pseudonym__plcontains=term)
            | Q(email__plcontains=term)
        )

    if contract_filter:
        authors = authors.filter(
            has_contract=contract_filter == "yes",
        )

    if contact_filter:
        authors = authors.filter(
            contact=contact_filter == "yes",
        )

    anthology_options = Anthology.objects.filter(
        Q(pk__in=authors.values('texts__anthology_id')) | Q(pk=selected_anthology_id))

    if selected_anthology_id is not None:
        authors = authors.filter(
            texts__anthology_id=selected_anthology_id,
        ).distinct()

    authors = authors.order_by(
        "last_name",
        "first_name",
        "pk",
    ).values(*AUTHOR_DISPLAY_FIELDS)

    # Najpierw stronicowanie w bazie, potem pobranie antologii
    # wyłącznie dla autorów widocznych na bieżącej stronie.
    page_obj = paginate_items(request, authors)
    page_authors = list(page_obj.object_list)
    author_ids = [author["pk"] for author in page_authors]
    anthologies_by_author = {
        author_id: []
        for author_id in author_ids
    }

    if author_ids:
        participations = (
            Text.objects.filter(
                authors__pk__in=author_ids,
                anthology__isnull=False,
            )
            .order_by(
                "anthology__title",
                "anthology_id",
                "authors__pk",
            )
            .values(
                "authors__pk",
                "anthology_id",
                "anthology__title",
            )
            .distinct()
        )

        for item in participations:
            anthologies_by_author[item["authors__pk"]].append(
                {
                    "pk": item["anthology_id"],
                    "title": item["anthology__title"],
                }
            )

    for author in page_authors:
        author["participating_anthologies"] = anthologies_by_author[
            author["pk"]
        ]

    page_obj.object_list = page_authors

    return render(
        request,
        "core/author_list.html",
        {
            "authors": page_obj,
            "page_obj": page_obj,
            "anthologies": anthology_options.order_by(
                "title",
                "pk",
            ).values("pk", "title"),
            "query": query,
            "contract_filter": contract_filter,
            "contact_filter": contact_filter,
            "selected_anthology_id": selected_anthology_id,
            "can_view_authors": True,
        },
    )


@never_cache
@login_required
@require_GET
@superuser_required
def author_detail(request, author_id):
    author = get_object_or_404(Author, pk=author_id)

    return _render_author_detail(request, author)


@never_cache
@login_required
@require_POST
@superuser_required
def add_author_note(request, author_id):
    form = AuthorNoteForm(request.POST)

    with transaction.atomic():
        author = get_object_or_404(
            Author.objects.select_for_update(),
            pk=author_id,
        )

        if form.is_valid():
            note = form.save(commit=False)
            note.author = author
            note.created_by = request.user
            note.save()
            saved = True
        else:
            saved = False

    if not saved:
        return _render_author_detail(
            request,
            author,
            form=form,
            status=400,
        )

    messages.success(request, "Dodano notatkę o autorze.")

    return redirect(
        "core:author_detail",
        author_id=author.pk,
    )