from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from authors.models import Author
from core.exports import export_texts_csv
from core.forms import (
    CoordinatorNoteForm,
    CoordinatorTextBulkActionForm,
    RestartWorkflowForm,
    StartStageForm,
    TextContentWarningsForm,
    TextNoteForm,
)
from core.pagination import paginate_items
from core.permissions import (
    can_perform_bulk_actions,
    can_restart_workflow,
    can_view_author_data,
    coordinator_required,
    is_coordinator,
    superuser_required,
    team_member_required,
)
from core.selectors.people import user_leave_information
from core.selectors.texts import (
    available_stages_for_user,
    my_texts_context,
    text_detail_context,
    text_list_context,
)
from core.services.texts import perform_bulk_text_action
from texts.models import Text
from workflow.models import WorkflowRoleAssignment


MAX_SELECTED_OBJECTS = 1000
MAX_DATABASE_ID = 9_223_372_036_854_775_807


def _selected_ids(data, field_name):
    """Odrzuca niepełny lub nieprawidłowy wybór zamiast pomijać błędy."""
    values = data.getlist(field_name)

    if not values:
        raise ValidationError("Zaznacz przynajmniej jeden element.")

    if len(values) > MAX_SELECTED_OBJECTS:
        raise ValidationError(
            f"Jednorazowo można zaznaczyć najwyżej "
            f"{MAX_SELECTED_OBJECTS} elementów."
        )

    selected_ids = set()

    for value in values:
        value = value.strip()

        if (
            not value
            or len(value) > 19
            or not value.isascii()
            or not value.isdecimal()
        ):
            raise ValidationError("Przesłano nieprawidłowy identyfikator.")

        object_id = int(value)

        if not 1 <= object_id <= MAX_DATABASE_ID:
            raise ValidationError("Przesłano nieprawidłowy identyfikator.")

        selected_ids.add(object_id)

    return sorted(selected_ids)


def _form_error_message(form):
    return " ".join(
        str(error)
        for errors in form.errors.values()
        for error in errors
    )


def _require_text_contributor(user, text):
    """Uprawnienia dotyczą wyłącznie bieżącego cyklu pracy."""
    if is_coordinator(user):
        return

    is_assigned = WorkflowRoleAssignment.objects.filter(
        text_id=text.pk,
        workflow_cycle=text.current_workflow_cycle,
        assigned_to_id=user.pk,
    ).exists()

    if not is_assigned:
        raise PermissionDenied(
            "Tę zmianę może wprowadzić osoba przypisana do tekstu "
            "lub koordynator."
        )


def _permission_context(user):
    return {
        "can_view_authors": can_view_author_data(user),
        "can_manage_authors": can_view_author_data(user),
        "can_perform_bulk_actions": can_perform_bulk_actions(user),
        "can_manage_workflow": is_coordinator(user),
        "can_restart_workflow": can_restart_workflow(user),
    }


def _render_text_detail(request, text, *, bound_forms=None, status=200):
    """
    Selektor przygotowuje historię, przydziały i dostępne akcje.

    Dane autora oraz dane identyfikujące autora recenzji źródłowej
    mogą trafić do kontekstu wyłącznie dla superusera. Selektor musi
    stosować tę zasadę również do zagnieżdżonych obiektów.
    """
    context = text_detail_context(
        user=request.user,
        text=text,
    )
    context.update(_permission_context(request.user))

    coordinator_access = is_coordinator(request.user)
    is_assigned = WorkflowRoleAssignment.objects.filter(
        text_id=text.pk,
        workflow_cycle=text.current_workflow_cycle,
        assigned_to_id=request.user.pk,
    ).exists()
    can_contribute = coordinator_access or is_assigned

    context.update(
        {
            "is_assigned": is_assigned,
            "can_add_note": can_contribute,
            "can_edit_content_warnings": can_contribute,
            "can_edit_coordinator_note": coordinator_access,
            "text_note_form": TextNoteForm() if can_contribute else None,
            "coordinator_note_form": (
                CoordinatorNoteForm(instance=text)
                if coordinator_access
                else None
            ),
            "text_content_warnings_form": (
                TextContentWarningsForm(instance=text)
                if can_contribute
                else None
            ),
            "start_stage_form": StartStageForm(),
            "restart_workflow_form": (
                RestartWorkflowForm()
                if can_restart_workflow(request.user)
                else None
            ),
        }
    )

    if bound_forms:
        context.update(bound_forms)

    return render(
        request,
        "core/assigned_text_detail.html",
        context,
        status=status,
    )


@never_cache
@login_required
@require_GET
@team_member_required
def text_list(request):
    """
    Selektor stosuje filtry i sortowanie z białej listy.

    Filtrowanie, wyszukiwanie i sortowanie po autorze jest dostępne
    wyłącznie dla superusera. Zwrócone teksty muszą być przygotowane
    do bezpiecznego wyświetlenia dla wskazanego użytkownika.
    """
    context = dict(
        text_list_context(
            user=request.user,
            params=request.GET,
        )
    )
    page_obj = paginate_items(request, context.pop("texts"))

    context.update(_permission_context(request.user))
    context.update(
        {
            "texts": page_obj,
            "page_obj": page_obj,
            "bulk_action_form": (
                CoordinatorTextBulkActionForm()
                if can_perform_bulk_actions(request.user)
                else None
            ),
        }
    )

    return render(request, "core/text_list.html", context)


@never_cache
@login_required
@require_GET
@team_member_required
def my_texts(request):
    selected_view = request.GET.get("view", "active").strip()

    if selected_view not in {"active", "waiting", "completed", "all"}:
        selected_view = "active"

    context = dict(
        my_texts_context(
            user=request.user,
            selected_view=selected_view,
        )
    )
    page_obj = paginate_items(request, context.pop("texts"))

    context.update(_permission_context(request.user))
    context.update(
        {
            "texts": page_obj,
            "page_obj": page_obj,
            "selected_view": selected_view,
        }
    )

    return render(request, "core/my_texts.html", context)


@never_cache
@login_required
@require_GET
@team_member_required
def assigned_text_detail(request, text_id):
    text = get_object_or_404(
        Text.objects.select_related("anthology"),
        pk=text_id,
    )

    return _render_text_detail(request, text)


@never_cache
@login_required
@require_GET
@team_member_required
def available_texts(request):
    available_stages = available_stages_for_user(user=request.user)
    page_obj = paginate_items(request, available_stages)

    context = _permission_context(request.user)
    context.update(
        {
            "available_stages": page_obj,
            "page_obj": page_obj,
            "start_stage_form": StartStageForm(),
            "leave_information": user_leave_information(request.user),
        }
    )

    return render(request, "core/available_texts.html", context)


@never_cache
@login_required
@require_POST
@superuser_required
def set_text_authors(request, text_id):
    try:
        author_ids = _selected_ids(request.POST, "authors")

        with transaction.atomic():
            text = get_object_or_404(
                Text.objects.select_for_update(),
                pk=text_id,
            )
            authors = list(
                Author.objects.select_for_update()
                .filter(pk__in=author_ids)
                .order_by("pk")
            )

            if len(authors) != len(author_ids):
                raise ValidationError(
                    "Nie odnaleziono wszystkich wskazanych autorów. "
                    "Odśwież stronę i ponów wybór."
                )

            text.authors.set(authors)

    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Zapisano autorów tekstu.")

    return redirect("core:assigned_text_detail", text_id=text_id)


@never_cache
@login_required
@require_POST
@team_member_required
def add_text_note(request, text_id):
    with transaction.atomic():
        text = get_object_or_404(
            Text.objects.select_for_update(),
            pk=text_id,
        )
        _require_text_contributor(request.user, text)

        form = TextNoteForm(request.POST)

        if form.is_valid():
            note = form.save(commit=False)
            note.text = text
            note.author = request.user
            note.save()
            saved = True
        else:
            saved = False

    if not saved:
        return _render_text_detail(
            request,
            text,
            bound_forms={"text_note_form": form},
            status=400,
        )

    messages.success(request, "Dodano notatkę.")
    return redirect("core:assigned_text_detail", text_id=text.pk)


@never_cache
@login_required
@require_POST
@coordinator_required
def update_coordinator_note(request, text_id):
    with transaction.atomic():
        text = get_object_or_404(
            Text.objects.select_for_update(),
            pk=text_id,
        )
        form = CoordinatorNoteForm(request.POST, instance=text)

        if form.is_valid():
            text.coordinator_note = form.cleaned_data["coordinator_note"]
            text.save(update_fields=["coordinator_note"])
            saved = True
        else:
            saved = False

    if not saved:
        text.refresh_from_db()

        return _render_text_detail(
            request,
            text,
            bound_forms={"coordinator_note_form": form},
            status=400,
        )

    messages.success(request, "Zapisano notatkę koordynatora.")
    return redirect("core:assigned_text_detail", text_id=text.pk)


@never_cache
@login_required
@require_POST
@team_member_required
def update_text_content_warnings(request, text_id):
    with transaction.atomic():
        text = get_object_or_404(
            Text.objects.select_for_update(),
            pk=text_id,
        )
        _require_text_contributor(request.user, text)

        form = TextContentWarningsForm(request.POST, instance=text)

        if form.is_valid():
            text.content_warnings = form.cleaned_data["content_warnings"]
            text.save(update_fields=["content_warnings"])
            saved = True
        else:
            saved = False

    if not saved:
        text.refresh_from_db()

        return _render_text_detail(
            request,
            text,
            bound_forms={"text_content_warnings_form": form},
            status=400,
        )

    messages.success(request, "Zapisano ostrzeżenia dotyczące treści.")
    return redirect("core:assigned_text_detail", text_id=text.pk)


@never_cache
@login_required
@require_POST
@superuser_required
def bulk_text_action(request):
    form = CoordinatorTextBulkActionForm(request.POST)

    try:
        text_ids = _selected_ids(request.POST, "selected_texts")
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("core:text_list")

    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return redirect("core:text_list")

    action = form.cleaned_data["action"]

    try:
        if action == CoordinatorTextBulkActionForm.Action.EXPORT:
            texts = list(
                Text.objects.filter(pk__in=text_ids)
                .select_related("anthology")
                .prefetch_related("authors")
                .order_by("anthology__title", "title", "pk")
            )

            if len(texts) != len(text_ids):
                raise ValidationError(
                    "Nie odnaleziono wszystkich zaznaczonych tekstów. "
                    "Odśwież listę i ponów wybór."
                )

            return export_texts_csv(
                user=request.user,
                texts=texts,
                filename="teksty.csv",
            )

        # Serwis ponownie sprawdza uprawnienia oraz kompletność wyboru.
        # Cała operacja jest atomowa: blokuje teksty w kolejności PK,
        # następnie przydziały z ich bieżących cykli. Przed zmianą ról
        # sprawdza aktywność osoby, wymagane role, konflikt weryfikatorów
        # i stan procesu. Błąd dowolnego tekstu wycofuje całą operację.
        changed_count = perform_bulk_text_action(
            user=request.user,
            text_ids=text_ids,
            action=action,
            anthology=form.cleaned_data.get("anthology"),
            role=form.cleaned_data.get("role"),
            assigned_to=form.cleaned_data.get("assigned_to"),
            note=form.cleaned_data.get("note", ""),
            note_is_important=form.cleaned_data.get(
                "note_is_important",
                False,
            ),
        )

    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("core:text_list")

    messages.success(
        request,
        f"Wykonano operację dla {changed_count} tekstów.",
    )
    return redirect("core:text_list")