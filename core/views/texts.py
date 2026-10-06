from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from authors.models import Author
from core.forms import (
    CoordinatorNoteForm,
    RestartWorkflowForm,
    StartStageForm,
    TextContentWarningsForm,
    TextNoteForm,
)
from core.pagination import paginate_items
from core.permissions import (
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
from texts.models import Text, TextNote
from core.tag_forms import TextTagsForm
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

    is_assigned = WorkflowRoleAssignment.objects.current_cycle().filter(
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
        "can_manage_workflow": is_coordinator(user),
        "can_restart_workflow": can_restart_workflow(user),
    }


@transaction.atomic
def _render_text_detail(request, text, *, bound_forms=None, status=200):
    """
    Selektor przygotowuje historię, przydziały i dostępne akcje.

    Selektor ogranicza dane autora i recenzji źródłowej do superusera.
    Widok udostępnia osobno wyłącznie adresy e-mail autorów tego tekstu
    osobom mającym przy nim przypisanie, również w historii.
    """
    text = (Text.objects.select_for_update() if request.method == "POST" else Text.objects).select_related("anthology").get(pk=text.pk)
    context = text_detail_context(
        user=request.user,
        text=text,
    )
    context.update(_permission_context(request.user))
    context['is_novel'] = bool(text.anthology_id and text.anthology.is_novel)
    context['is_translation'] = bool(text.anthology_id and text.anthology.is_translated)
    if context['is_translation']:
        from core.translation_forms import TranslationForm
        from texts.models import TextTranslation
        record = TextTranslation.objects.filter(text=text).first()
        context['translation_form'] = TranslationForm(instance=record) if request.user.is_superuser else None
        context['translators'] = list(record.translators.all()) if record else []
        context['original_verifier'] = record.original_verifier if record else ''
    if request.user.is_superuser:
        from core.supervision import text_credit_groups
        context['text_credits'] = text_credit_groups(text)
    from core.workflow_tokens import make_token
    context['workflow_token'] = make_token(text, request.user)

    coordinator_access = is_coordinator(request.user)
    is_assigned = WorkflowRoleAssignment.objects.current_cycle().filter(
        text_id=text.pk,
        workflow_cycle=text.current_workflow_cycle,
        assigned_to_id=request.user.pk,
    ).exists()
    can_contribute = coordinator_access or is_assigned
    from core.file_forms import TextFileForm
    context['file_url'] = text.file_url
    context['text_tags_form'] = TextTagsForm(instance=text)
    from core.views.audiobooks import AudiobookForm
    from illustrations.models import Illustration
    from core.permissions import can_view_illustrations
    context['audiobook_form'] = AudiobookForm(instance=text)
    context['text_illustration'] = Illustration.objects.select_related('illustrator').filter(text_id=text.pk).first()
    context['can_open_text_illustration'] = can_view_illustrations(request.user)
    context['anthology_illustrated'] = bool(text.anthology_id and text.anthology.has_illustrations)
    context['text_file_form'] = TextFileForm(instance=text) if request.user.is_superuser else None
    context['dropbox_chooser_app_key'] = getattr(settings, 'DROPBOX_CHOOSER_APP_KEY', '') if request.user.is_superuser else ''
    # Only this text's email addresses are revealed, never source-review identity.
    email_access = coordinator_access or (
        WorkflowRoleAssignment.objects.filter(text=text, assigned_to=request.user).exists()
    )
    contacts = record.foreign_authors if context['is_translation'] and record else text.authors
    context["author_emails"] = list(contacts.exclude(email__isnull=True).exclude(email="").values_list("email", flat=True)) if email_access else []

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
                RestartWorkflowForm(text=text)
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
        }
    )

    return render(request, "core/text_list.html", context)


@never_cache
@login_required
@require_GET
@team_member_required
def my_texts(request):
    selected_view = request.GET.get("view", "active").strip()

    context = dict(
        my_texts_context(
            user=request.user,
            selected_view=selected_view,
            params=request.GET,
        )
    )
    page_obj = paginate_items(request, context.pop("texts"))

    context.update(_permission_context(request.user))
    context.update(
        {
            "texts": page_obj,
            "page_obj": page_obj,
            "mobile_sort_columns": [(label, page_obj.sort_columns[label]) for label in
                ("Antologia", "Tytuł", "Autorzy", "Twoje role", "Etap", "Twoja praca")
                if label in page_obj.sort_columns],
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

    if text.anthology_id and text.anthology.is_translated:
        return redirect('core:translation_detail', text_id=text.pk)
    return _render_text_detail(request, text)


@never_cache
@login_required
@require_POST
@team_member_required
def update_text_tags(request, text_id):
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
        if text.anthology_id and text.anthology.is_novel:
            messages.error(request, 'Tagi i gatunek rozdziałów zmienia się w podglądzie powieści.')
            return redirect('core:novel_detail', novel_id=text.anthology_id)
        form = TextTagsForm(request.POST, instance=text)
        if form.is_valid():
            from texts.vocabulary import canonicalize
            text.tags = canonicalize(form.cleaned_data['tags'], 'tag', register=True)
            # An older browser tab may still submit the tags-only form.
            fields = ['tags']
            if 'genre' in request.POST:
                text.genre = canonicalize(form.cleaned_data['genre'], 'genre', register=True)
                fields.append('genre')
            text.save(update_fields=fields)
            messages.success(request, 'Zapisano tagi i gatunek tekstu.')
            return redirect('core:assigned_text_detail', text_id=text.pk)
        return _render_text_detail(request, text, bound_forms={'text_tags_form': form}, status=400)


@never_cache
@login_required
@require_GET
@team_member_required
def available_texts(request):
    params = request.GET.copy()
    for key in ("status", "sort", "hide_ready"):
        params.pop(key, None)
    available_stages, filters = available_stages_for_user(user=request.user, params=params, with_filters=True)
    page_obj = paginate_items(request, available_stages)

    context = dict(filters)
    context.update(_permission_context(request.user))
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
            if text.anthology_id and text.anthology.is_novel:
                raise ValidationError('Autorów rozdziału zmień w podglądzie całej powieści.')
            if text.anthology_id and text.anthology.is_translated:
                raise ValidationError('Autora zagranicznego zmień w formularzu tłumaczenia.')
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

        if text.anthology_id and text.anthology.is_novel:
            raise PermissionDenied('Ostrzeżenia są wspólne dla całej powieści. Zmień je w jej podglądzie.')

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


def _require_note_owner_or_coordinator(user, note):
    if note.author_id != user.pk and not is_coordinator(user):
        raise PermissionDenied('Notatkę może zmienić jej autor lub koordynator.')


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def edit_text_note(request, text_id, note_id):
    with transaction.atomic():
        text = get_object_or_404((Text.objects.select_for_update() if request.method == "POST" else Text.objects), pk=text_id)
        note = get_object_or_404((TextNote.objects.select_for_update() if request.method == "POST" else TextNote.objects), pk=note_id, text=text)
        _require_note_owner_or_coordinator(request.user, note)
        form = TextNoteForm(request.POST if request.method == 'POST' else None, instance=note)
        form.fields["content"].label = "Treść notatki"
        if request.method == 'POST' and form.is_valid():
            form.save()
            messages.success(request, 'Zapisano notatkę.')
            return redirect('core:assigned_text_detail', text_id=text.pk)
    return render(request, 'core/text_note_edit.html', {'form': form, 'text': {'pk': text.pk, 'title': text.title}}, status=400 if form.errors else 200)


@never_cache
@login_required
@require_POST
@team_member_required
def delete_text_note(request, text_id, note_id):
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
        note = get_object_or_404(TextNote.objects.select_for_update(), pk=note_id, text=text)
        _require_note_owner_or_coordinator(request.user, note)
        note.delete()
    messages.success(request, 'Usunięto notatkę.')
    return redirect('core:assigned_text_detail', text_id=text.pk)


@never_cache
@login_required
@require_POST
@superuser_required
def update_text_file(request, text_id):
    from core.file_forms import TextFileForm
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
        if text.anthology_id and text.anthology.is_novel:
            raise PermissionDenied('Folder jest wspólny dla całej powieści. Zmień go w jej podglądzie.')
        form = TextFileForm(request.POST, instance=text)
        if form.is_valid():
            form.save()
            messages.success(request, 'Zapisano link do folderu Dropbox.')
            return redirect('core:assigned_text_detail', text_id=text.pk)
    return _render_text_detail(request, text, bound_forms={'text_file_form':form}, status=400)


@never_cache
@login_required
@superuser_required
@require_http_methods(['GET','POST'])
def link_text_review(request, text_id):
    from django.db.models import Q
    from core.source_reviews import linkable_reviews, suggested_review_ids, link_source_review
    text = get_object_or_404(Text.objects.select_related('anthology'),pk=text_id)
    query = request.GET.get('q','').strip()
    candidates = linkable_reviews(text).select_related("author").prefetch_related("coauthors")
    suggestions = suggested_review_ids(text)
    if query:
        candidates = candidates.filter(Q(title__plcontains=query) | Q(author_first_name__plcontains=query) | Q(author_last_name__plcontains=query) | Q(email__plcontains=query))
    if request.method == 'POST':
        try:
            review_id = int(request.POST.get('review_id',''))
            link_source_review(user=request.user,text_id=text.pk,review_id=review_id,confirm_mismatch=request.POST.get('confirm_mismatch') == 'on')
        except (ValueError,ValidationError) as error:
            messages.error(request,' '.join(error.messages) if isinstance(error,ValidationError) else 'Wybierz zgłoszenie.')
        else:
            messages.success(request,'Zapisano powiązanie z recenzjami. Pozostanie zachowane po zmianie tytułu.')
            return redirect('core:assigned_text_detail',text_id=text.pk)
    from django.db.models import Case,When,IntegerField,Value
    candidates=candidates.annotate(suggestion_order=Case(When(pk__in=suggestions,then=Value(0)),default=Value(1),output_field=IntegerField())).order_by('suggestion_order','title','pk')
    candidates = candidates.annotate(source_information=Case(
        When(copied_text_id=text.pk, then=Value('Obecne powiązanie')),
        When(pk__in=suggestions, then=Value('Podpowiedź')),
        When(old_reviews=True, then=Value('Archiwum')), default=Value('')))
    page=paginate_items(request,candidates)
    return render(request,'core/link_text_review.html',{'text':text,'query':query,'candidates':page,'page_obj':page,'suggested_ids':suggestions})
