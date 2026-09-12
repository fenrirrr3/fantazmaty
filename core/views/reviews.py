from django.contrib import messages
import hashlib
import json
from django.core import signing
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from core.exports import export_reviews_csv
from core.forms import (
    AuthorNotificationForm,
    CoordinatorReviewBulkActionForm,
    ReviewBulkImportForm,
    ReviewContentWarningsForm,
    ReviewerOpinionForm,
)
from core.pagination import paginate_items
from core.permissions import (
    can_import_reviews,
    can_manage_reviews,
    can_perform_bulk_actions,
    can_self_assign_reviews,
    can_view_author_data,
    coordinator_required,
    superuser_required,
    team_member_required,
)
from core.selectors.reviews import review_list_context
from core.services.reviews import (
    assign_reviewer as assign_reviewer_service,
    change_review_status,
    copy_review_to_text as copy_review_to_text_service,
    import_reviews,
    perform_bulk_review_action,
    save_author_notification,
    save_review_content_warnings,
    save_reviewer_opinion,
    unassign_reviewer as unassign_reviewer_service,
)
from texts.models import Review, ReviewAssignment, Reviewers


CLOSED_REVIEW_STATUSES = frozenset(
    {
        Review.Status.ACCEPTED,
        Review.Status.REJECTED,
        Review.Status.WITHDRAWN,
    }
)
MAX_SELECTED_REVIEWS = 1000
MAX_DATABASE_ID = 9_223_372_036_854_775_807

# Serwisy mutujące ponownie sprawdzają uprawnienia i aktualny stan.
# Blokują najpierw Review, następnie jego przydziały, i wykonują całą
# zmianę atomowo. Recenzje old_reviews nie podlegają tym operacjom.
# Widok nie zastępuje tej walidacji sprawdzeniem wykonanym przed zapisem.


def _get_review(user, review_id):
    queryset = Review.objects.visible_to(user).select_related("anthology")

    if can_view_author_data(user):
        queryset = queryset.select_related("author")
    else:
        queryset = queryset.filter(old_reviews=False)

    return get_object_or_404(queryset, pk=review_id)


def _detail_redirect(review_id):
    return redirect(
        "core:assigned_review_detail",
        review_id=review_id,
    )


def _review_is_locked(review):
    return review.old_reviews or review.status in CLOSED_REVIEW_STATUSES


def _require_open_review(review):
    if _review_is_locked(review):
        raise ValidationError(
            "Archiwalna lub zakończona recenzja jest zablokowana."
        )


def _own_assignment(review, user):
    return ReviewAssignment.objects.filter(
        review_id=review.pk,
        user_id=user.pk,
    ).first()


def _require_assigned_reviewer(review, user):
    assignment = _own_assignment(review, user)

    if assignment is None:
        raise PermissionDenied(
            "Opinię może zapisać wyłącznie przypisany recenzent."
        )

    return assignment


def _require_review_contributor(review, user):
    if not can_manage_reviews(user) and _own_assignment(review, user) is None:
        raise PermissionDenied(
            "Ostrzeżenia dotyczące treści może zmieniać "
            "przypisany recenzent lub koordynator."
        )


def _selected_review_ids(data):
    values = data.getlist("selected_reviews")

    if not values:
        raise ValidationError("Zaznacz przynajmniej jedno zgłoszenie.")

    if len(values) > MAX_SELECTED_REVIEWS:
        raise ValidationError(
            f"Jednorazowo można zaznaczyć najwyżej "
            f"{MAX_SELECTED_REVIEWS} zgłoszeń."
        )

    result = set()

    for value in values:
        value = value.strip()

        if (
            not value
            or len(value) > 19
            or not value.isascii()
            or not value.isdecimal()
        ):
            raise ValidationError("Przesłano nieprawidłowy identyfikator.")

        review_id = int(value)

        if not 1 <= review_id <= MAX_DATABASE_ID:
            raise ValidationError("Przesłano nieprawidłowy identyfikator.")

        result.add(review_id)

    return sorted(result)


def _form_error_message(form):
    return " ".join(
        str(error)
        for errors in form.errors.values()
        for error in errors
    )


def _service_error_message(user, error):
    # Komunikaty walidacji modeli mogą zawierać dane autora.
    if can_view_author_data(user):
        return " ".join(error.messages)

    return (
        "Nie udało się wykonać operacji. Sprawdź aktualny status "
        "recenzji, przypisanie oraz wprowadzone dane."
    )


def _permission_context(user):
    can_view_authors = can_view_author_data(user)

    return {
        "can_view_authors": can_view_authors,
        "can_view_author_data": can_view_authors,
        "can_view_review_author": can_view_authors,
        "can_view_all_reviews": can_view_authors,
        "can_import_reviews": can_import_reviews(user),
        "can_manage_review": can_manage_reviews(user),
        "can_perform_bulk_actions": can_perform_bulk_actions(user),
    }


def _review_template_data(review, *, include_author):
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


def _render_review_detail(
    request,
    review,
    *,
    bound_forms=None,
    status=200,
):
    include_author = can_view_author_data(request.user)
    is_locked = _review_is_locked(review)
    manager_access = can_manage_reviews(request.user)

    assignments = list(
        ReviewAssignment.objects.filter(review_id=review.pk)
        .select_related("user")
        .order_by("position", "pk")
    )
    own_assignment = next(
        (
            assignment
            for assignment in assignments
            if assignment.user_id == request.user.pk
        ),
        None,
    )

    opinions = []

    for assignment in assignments:
        reviewer = assignment.user
        reviewer_name = (
            reviewer.get_full_name() or "Nieuzupełnione dane"
            if reviewer is not None
            else "Usunięte konto"
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
                "is_own": assignment.user_id == request.user.pk,
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

    can_contribute = (
        not is_locked
        and (manager_access or own_assignment is not None)
    )
    has_free_slot = len(assignments) < ReviewAssignment.MAX_REVIEWERS
    opinion_form = None

    if own_assignment is not None and not is_locked:
        opinion_form = ReviewerOpinionForm(
            initial={
                "opinion": (
                    ""
                    if own_assignment.opinion == Reviewers.Opinion.READING
                    else own_assignment.opinion
                ),
                "notes": own_assignment.notes,
                "content_warnings": review.content_warnings,
            }
        )

    review_data = _review_template_data(
        review,
        include_author=include_author,
    )
    can_notify_author = (
        include_author
        and not review.old_reviews
        and review.status in (Review.Status.ACCEPTED, Review.Status.REJECTED)
        and (review.decision_at is None or review.decision_at <= timezone.localdate())
        and review.copied_text_id is None
    )

    context = _permission_context(request.user)
    context.update(
        {
            "review": review_data,
            "user_slot": (
                own_assignment.position if own_assignment else None
            ),
            "user_status": (
                own_assignment.get_opinion_display()
                if own_assignment
                else None
            ),
            "user_status_date": (
                own_assignment.opinion_changed_at
                if own_assignment
                else None
            ),
            "user_notes": own_assignment.notes if own_assignment else None,
            "opinion_form": opinion_form,
            "other_reviewer_opinions": [
                item for item in opinions if not item["is_own"]
            ],
            "all_reviewer_opinions": opinions,
            "opinion_summary": opinion_summary,
            "general_notes": (
                Reviewers.objects.filter(review_id=review.pk)
                .values_list("general_notes", flat=True)
                .first()
                or ""
            ),
            "is_review_locked": is_locked,
            "can_self_assign_review": (
                can_self_assign_reviews(request.user)
                and not is_locked
                and own_assignment is None
                and has_free_slot
            ),
            "can_unassign_review": (
                own_assignment is not None and not is_locked
            ),
            "reviewers_have_free_slot": has_free_slot and not is_locked,
            "can_edit_content_warnings": can_contribute,
            "review_content_warnings_form": (
                ReviewContentWarningsForm(
                    initial={"content_warnings": review.content_warnings}
                )
                if can_contribute
                else None
            ),
            "can_change_review_status": (
                manager_access
                and not review.old_reviews
                and review.copied_text_id is None
                and (
                    review.status != Review.Status.WITHDRAWN
                    or include_author
                )
            ),
            "can_notify_author": can_notify_author,
            "author_notification_form": (
                AuthorNotificationForm(
                    initial={
                        "author_notified": bool(review.author_notified_at),
                        "author_notified_at": review.author_notified_at,
                    }
                )
                if can_notify_author
                else None
            ),
            "can_copy_review_to_text": (
                can_notify_author
                and review.status == Review.Status.ACCEPTED
                and review.author_notified_at is not None
            ),
            "is_copied_to_text": review.copied_text_id is not None,
            "matching_author": (
                review_data.get("author") if include_author else None
            ),
            "author_has_contract": (
                bool(review.author and review.author.has_contract)
                if include_author
                else False
            ),
        }
    )

    if include_author and not review.author_id:
        from authors.models import Author
        from texts.services import normalize_author_name
        matches = list(Author.objects.filter(email__iexact=review.email.strip()).order_by("pk")[:2])
        if len(matches) == 1 and normalize_author_name(f"{matches[0].first_name} {matches[0].last_name}") == normalize_author_name(f"{review.author_first_name} {review.author_last_name}"):
            context["matching_author"] = {"pk": matches[0].pk, "has_contract": matches[0].has_contract}
            context["author_has_contract"] = matches[0].has_contract

    if bound_forms:
        context.update(bound_forms)

    return render(
        request,
        "core/assigned_review_detail.html",
        context,
        status=status,
    )


@never_cache
@login_required
@require_GET
@team_member_required
def review_list(request):
    # Selektor stosuje old_reviews, filtry i sortowanie z białej listy.
    # Dla osób innych niż superuser zwraca wyłącznie bieżące recenzje,
    # bez danych autora i bez możliwości wyszukiwania po tych danych.
    # Elementy kontekstu muszą być bezpiecznymi projekcjami danych,
    # także w zagnieżdżonych strukturach.
    context = dict(
        review_list_context(
            user=request.user,
            params=request.GET,
        )
    )
    page_obj = paginate_items(request, context.pop("reviews"))

    context.update(_permission_context(request.user))
    context.update(
        {
            "reviews": page_obj,
            "page_obj": page_obj,
            "status_choices": Review.Status.choices,
            "bulk_action_form": (
                CoordinatorReviewBulkActionForm()
                if can_perform_bulk_actions(request.user)
                else None
            ),
        }
    )

    return render(request, "core/review_list.html", context)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@superuser_required
def review_bulk_import(request):
    form = ReviewBulkImportForm(
        request.POST if request.method == "POST" else None,
        user=request.user,
    )

    valid = form.is_valid() if request.method == "POST" else False
    preview_payload = [request.user.pk, hashlib.sha256(json.dumps([
        request.POST.get("anthology", ""), request.POST.get("records", "").strip()
    ], ensure_ascii=False).encode()).hexdigest()]
    confirmed = False
    if request.POST.get("import_action") == "import":
        try:
            confirmed = signing.loads(request.POST.get("preview_token", ""), salt="review-preview", max_age=3600) == preview_payload
        except signing.BadSignature:
            pass
    if request.method == "POST" and valid and confirmed:
        try:
            # Serwis ponownie waliduje formularz i aktualne ostrzeżenia
            # w transakcji. Sprawdza podpis, użytkownika, nabór, rekordy
            # oraz potwierdzenie ostrzeżeń, zanim zapisze jakikolwiek
            # rekord. Zmiana ostrzeżeń wymaga ponownego potwierdzenia.
            # Zwraca liczbę utworzonych recenzji.
            imported_count = import_reviews(
                user=request.user,
                form=form,
            )
        except ValidationError as error:
            form.add_error(None, " ".join(error.messages))
        else:
            messages.success(
                request,
                f"Dodano zgłoszenia: {imported_count}.",
            )
            return redirect("core:review_list")

    preview_rows = []
    if request.method == "POST":
        from texts.services import find_matching_authors
        records = {row["line_number"]: row for row in getattr(form, "preview_records", form.parsed_records)}
        all_errors = [str(error) for errors in form.errors.values() for error in errors]
        for line, raw in enumerate(request.POST.get("records", "").splitlines(), 1):
            if not raw.strip():
                continue
            if len(preview_rows) >= 1000:
                break
            row = records.get(line)
            warnings = [w for w in form.import_warnings if w.startswith(f"Wiersz {line}:")]
            errors = [e for e in all_errors if e.startswith((f"Wiersz {line}:", f"Wiersz {line},"))]
            matched = list(find_matching_authors(email=row["email"])[:2]) if row else []
            preview_rows.append({"line": line, "record": row, "raw": raw, "warnings": warnings,
                "errors": errors, "matched": matched, "ambiguous": len(matched) > 1})
    return render(
        request,
        "core/review_bulk_import.html",
        {
            "form": form,
            "import_warnings": getattr(form, "import_warnings", []),
            "preview_rows": preview_rows,
            "preview_token": signing.dumps(preview_payload, salt="review-preview") if valid else "",
            "can_import": valid,
        },
        status=400 if request.method == "POST" and not valid else 200,
    )


@never_cache
@login_required
@require_POST
@team_member_required
def assign_reviewer(request, review_id):
    if not can_self_assign_reviews(request.user):
        raise PermissionDenied(
            "Do recenzji mogą przypisywać się osoby z rolą recenzenta."
        )

    review = _get_review(request.user, review_id)

    try:
        _require_open_review(review)
        assign_reviewer_service(
            user=request.user,
            review_id=review.pk,
        )
    except ValidationError as error:
        messages.error(
            request,
            _service_error_message(request.user, error),
        )
    else:
        messages.success(request, "Przypisano cię do recenzji.")

    return _detail_redirect(review.pk)


@never_cache
@login_required
@require_POST
@team_member_required
def unassign_reviewer(request, review_id):
    review = _get_review(request.user, review_id)

    try:
        _require_open_review(review)
        unassign_reviewer_service(
            user=request.user,
            review_id=review.pk,
        )
    except ValidationError as error:
        messages.error(
            request,
            _service_error_message(request.user, error),
        )
    else:
        messages.success(request, "Usunięto twoje przypisanie do recenzji.")

    return _detail_redirect(review.pk)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def assigned_review_detail(request, review_id):
    review = _get_review(request.user, review_id)

    if request.method == "GET":
        return _render_review_detail(request, review)

    _require_assigned_reviewer(review, request.user)

    if _review_is_locked(review):
        messages.error(
            request,
            "Archiwalna lub zakończona recenzja jest zablokowana.",
        )
        return _detail_redirect(review.pk)

    form = ReviewerOpinionForm(request.POST)

    if form.is_valid():
        try:
            save_reviewer_opinion(
                user=request.user,
                review_id=review.pk,
                opinion=form.cleaned_data["opinion"],
                notes=form.cleaned_data["notes"],
                content_warnings=form.cleaned_data["content_warnings"],
            )
        except ValidationError as error:
            form.add_error(
                None,
                _service_error_message(request.user, error),
            )
        else:
            messages.success(request, "Zapisano opinię.")
            return _detail_redirect(review.pk)

    review = _get_review(request.user, review_id)

    return _render_review_detail(
        request,
        review,
        bound_forms={"opinion_form": form},
        status=400,
    )


@never_cache
@login_required
@require_POST
@team_member_required
def update_review_content_warnings(request, review_id):
    review = _get_review(request.user, review_id)
    _require_review_contributor(review, request.user)

    if _review_is_locked(review):
        messages.error(
            request,
            "Archiwalna lub zakończona recenzja jest zablokowana.",
        )
        return _detail_redirect(review.pk)

    # Formularz nie otrzymuje instancji zawierającej dane autora.
    form = ReviewContentWarningsForm(request.POST)

    if form.is_valid():
        try:
            save_review_content_warnings(
                user=request.user,
                review_id=review.pk,
                content_warnings=form.cleaned_data["content_warnings"],
            )
        except ValidationError as error:
            form.add_error(
                None,
                _service_error_message(request.user, error),
            )
        else:
            messages.success(request, "Zapisano ostrzeżenia dotyczące treści.")
            return _detail_redirect(review.pk)

    review = _get_review(request.user, review_id)

    return _render_review_detail(
        request,
        review,
        bound_forms={"review_content_warnings_form": form},
        status=400,
    )


@never_cache
@login_required
@require_POST
@superuser_required
def update_author_notification(request, review_id):
    review = _get_review(request.user, review_id)
    form = AuthorNotificationForm(request.POST)

    if form.is_valid():
        try:
            save_author_notification(
                user=request.user,
                review_id=review.pk,
                author_notified_at=form.cleaned_data["author_notified_at"],
            )
        except ValidationError as error:
            form.add_error(None, " ".join(error.messages))
        else:
            messages.success(request, "Zapisano oznaczenie powiadomienia autora.")
            return _detail_redirect(review.pk)

    review = _get_review(request.user, review_id)

    return _render_review_detail(
        request,
        review,
        bound_forms={"author_notification_form": form},
        status=400,
    )


@never_cache
@login_required
@require_POST
@coordinator_required
def update_review_status(request, review_id):
    review = _get_review(request.user, review_id)
    new_status = request.POST.get("status", "").strip()

    if new_status not in {
        Review.Status.ACCEPTED,
        Review.Status.REJECTED,
    }:
        messages.error(request, "Wybierz przyjęcie albo odrzucenie tekstu.")
        return _detail_redirect(review.pk)

    try:
        change_review_status(
            user=request.user,
            review_id=review.pk,
            new_status=new_status,
        )
    except ValidationError as error:
        messages.error(
            request,
            _service_error_message(request.user, error),
        )
    else:
        messages.success(request, "Zapisano decyzję dotyczącą tekstu.")

    return _detail_redirect(review.pk)


@never_cache
@login_required
@require_POST
@superuser_required
def copy_review_to_text(request, review_id):
    review = _get_review(request.user, review_id)

    try:
        # Serwis wymaga przyjętej, niearchiwalnej recenzji,
        # powiadomienia autora i potwierdzonej umowy. Tworzy tekst,
        # powiązanie autora i początek procesu w jednej transakcji.
        # Ponowienie żądania nie tworzy kolejnego tekstu.
        copy_review_to_text_service(
            user=request.user,
            review_id=review.pk,
            contract_received=request.POST.get("contract_received") == "yes",
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Tekst jest dodany do procesu wydawniczego.")

    return _detail_redirect(review.pk)


@never_cache
@login_required
@require_POST
@superuser_required
def bulk_review_action(request):
    form = CoordinatorReviewBulkActionForm(request.POST)

    try:
        review_ids = _selected_review_ids(request.POST)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("core:review_list")

    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return redirect("core:review_list")

    action = form.cleaned_data["action"]

    try:
        if action == CoordinatorReviewBulkActionForm.Action.EXPORT:
            reviews = list(
                Review.objects.filter(pk__in=review_ids)
                .select_related("anthology", "author")
                .order_by("anthology__title", "title", "pk")
            )

            if len(reviews) != len(review_ids):
                raise ValidationError(
                    "Nie odnaleziono wszystkich zaznaczonych zgłoszeń. "
                    "Odśwież listę i ponów wybór."
                )

            return export_reviews_csv(
                user=request.user,
                reviews=reviews,
                filename="recenzje.csv",
            )

        # Serwis odrzuca cały wybór, jeśli zawiera archiwalne,
        # nieistniejące lub niedopuszczające zmiany zgłoszenie.
        # Stosuje te same reguły przejścia statusu co operacja pojedyncza.
        changed_count = perform_bulk_review_action(
            user=request.user,
            review_ids=review_ids,
            action=action,
            new_status=form.cleaned_data["status"],
        )

    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("core:review_list")

    messages.success(
        request,
        f"Wykonano operację dla {changed_count} zgłoszeń.",
    )
    return redirect("core:review_list")


@never_cache
@login_required
@require_GET
@team_member_required
def my_reviews(request):
    from core.permissions import is_reviewer
    from django.core.exceptions import PermissionDenied
    if not is_reviewer(request.user):
        raise PermissionDenied("Moje recenzje są dostępne tylko dla recenzentów.")
    view = request.GET.get("view", "active")
    if view not in {"active", "waiting", "completed", "all"}:
        view = "active"
    rows = ReviewAssignment.objects.filter(user=request.user, review__old_reviews=False, review__is_hidden=False).select_related("review__anthology").order_by("-assigned_at", "-pk")
    if view == "active":
        rows = rows.filter(opinion="reading", review__status__in=("new", "in_review"))
    elif view == "waiting":
        rows = rows.filter(opinion="", review__status__in=("new", "in_review"))
    elif view == "completed":
        rows = rows.exclude(opinion__in=("", "reading"))
    query = request.GET.get("q", "").strip()[:255]
    for term in query.split():
        rows = rows.filter(Q(review__title__plcontains=term) | Q(review__anthology__title__plcontains=term))
    page = paginate_items(request, rows)
    return render(request, "core/my_reviews.html", {"assignments": page, "page_obj": page, "selected_view": view, "query": query})
