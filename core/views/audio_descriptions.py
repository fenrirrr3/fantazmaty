from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from core.edit_policy import edit_policy
from core.models import AudioDescription, AudioDescriptionNote, EditRevision
from core.pagination import paginate_items
from core.permissions import team_member_required, get_active_person_profile, is_coordinator, is_team_member
from core.table_sorting import DisplayTable
from people.models import Person
from texts.models import Anthology, AnthologyTask
from core.anthology_tasks import TaskPeopleChoice, TaskPersonChoice, task_people


def books():
    return Anthology.objects.filter(is_novel=False).exclude(status=Anthology.Status.ABANDONED)


def may_edit(user, book):
    if is_coordinator(user):
        return True
    person = get_active_person_profile(user)
    if person is None:
        return False
    return (
        book.production_tasks.filter(task_type="audio_description", assigned_to=person).exists()
        or AudioDescription.objects.filter(anthology=book, controllers=person).exists()
    )


def require_edit(request, book, kwargs):
    if book is None or not may_edit(request.user, book):
        raise PermissionDenied("Edycja wymaga przypisania do audiodeskrypcji lub kontroli.")


def may_write(user):
    """Treść i uwagi zapisuje każdy członek zespołu; etap i przypisanie – wykonawcy i koordynator."""
    return is_coordinator(user) or is_team_member(user)


def require_write(request, book, kwargs):
    if book is None or not may_write(request.user):
        raise PermissionDenied("Zapis treści i uwag wymaga aktywnego profilu członka zespołu.")


def require_action(request, book, kwargs):
    """Treść i uwagi – każdy członek zespołu; etap i przypisanie – osoby przypisane."""
    if request.POST.get("action", "assignment") in ("content", "note"):
        require_write(request, book, kwargs)
    else:
        require_edit(request, book, kwargs)


class DescriptionForm(forms.ModelForm):
    """Coordinator assignment only; the task status is managed on the anthology page."""
    assigned_to = TaskPersonChoice(label="Kto pisze", queryset=Person.objects.none(), required=False)
    controllers = TaskPeopleChoice(
        label="Konsultacja – osoby do kontroli",
        queryset=Person.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"data-person-multiple": "true"}),
    )

    class Meta:
        model = AnthologyTask
        fields = ("assigned_to",)

    def __init__(self, *args, description=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.description = description
        current = list(description.controllers.values_list("pk", flat=True)) if description else []
        self.fields["controllers"].queryset = task_people(*current)
        self.fields["controllers"].initial = current
        self.fields["assigned_to"].queryset = task_people(self.instance.assigned_to_id)
        self.fields["assigned_to"].widget.attrs["data-searchable-person"] = "true"
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        values = super().clean()
        if self.instance.status == AnthologyTask.Status.READY:
            if values.get("assigned_to") != self.instance.assigned_to:
                raise forms.ValidationError("Audiodeskrypcja jest gotowa. Wykonawcę zmienisz tylko w panelu admina.")
        else:
            # Assignment opens or closes the commission; "Gotowe" comes only from the work stage.
            self.instance.status = (AnthologyTask.Status.COMMISSIONED if values.get("assigned_to")
                                    else AnthologyTask.Status.NOT_COMMISSIONED)
        return values


class StageForm(forms.ModelForm):
    class Meta:
        model = AudioDescription
        fields = ("stage",)
        widgets = {"stage": forms.Select(attrs={"class": "form-control"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.was_completed = self.instance.pk and AudioDescription.objects.filter(
            pk=self.instance.pk, stage=AudioDescription.Stage.COMPLETED).exists()

    def clean_stage(self):
        stage = self.cleaned_data["stage"]
        if self.was_completed and stage != AudioDescription.Stage.COMPLETED:
            raise forms.ValidationError("Zakończonej audiodeskrypcji nie można cofnąć. Zmiana tylko w panelu admina.")
        return stage


class ContentForm(forms.ModelForm):
    class Meta:
        model = AudioDescription
        fields = ("content",)
        widgets = {"content": forms.Textarea(attrs={"rows": 14, "class": "form-control"})}


class NoteForm(forms.Form):
    note = forms.CharField(
        label="Dodaj uwagę", widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"})
    )


@never_cache
@login_required
@require_GET
@team_member_required
def audio_descriptions(request):
    query = request.GET.get("q", "").strip()[:200]
    selected_status = request.GET.get("status", "")
    hide_completed = request.GET.get("hide_completed") == "1"
    tasks = (
        AnthologyTask.objects.filter(task_type="audio_description", anthology__in=books())
        .select_related("anthology", "assigned_to", "anthology__audio_description")
        .prefetch_related("anthology__audio_description__controllers")
    )
    if selected_status:
        tasks = tasks.filter(status=selected_status)
    if hide_completed:
        tasks = tasks.exclude(status__in=AnthologyTask.DONE_STATUSES)
    person = get_active_person_profile(request.user)
    versions = dict(
        EditRevision.objects.filter(
            model_label="texts.anthology", object_id__in=tasks.values("anthology_id")
        ).values_list("object_id", "version")
    )
    rows = []
    for task in tasks:
        description = getattr(task.anthology, "audio_description", None)
        controllers = list(description.controllers.all()) if description else []
        if query and not all(
            term
            in f"{task.anthology} {task.assigned_to or ''} {' '.join(str(p) for p in controllers)}".casefold()
            for term in query.casefold().split()
        ):
            continue
        rows.append(
            {
                "task": task,
                "book": task.anthology,
                "controllers": controllers,
                "can_claim": person is not None
                and task.assigned_to_id is None
                and task.status == "not_commissioned",
                "token": signing.dumps(
                    [
                        request.user.pk,
                        f"texts.anthology:{task.anthology_id}",
                        versions.get(task.anthology_id, 0),
                    ],
                    salt="cms-edit-version",
                ),
            }
        )
    page = paginate_items(
        request,
        DisplayTable(
            rows,
            {
                "Antologia": ("anthology", lambda row: row["book"].title),
                "Kto pisze": ("person", lambda row: str(row["task"].assigned_to or "")),
                "Status": ("status", lambda row: row["task"].get_status_display()),
                "Konsultacja": (
                    "controllers",
                    lambda row: ", ".join(str(p) for p in row["controllers"]),
                ),
            },
        ),
    )
    return render(
        request,
        "core/audio_descriptions.html",
        {
            "page_obj": page,
            "rows": page,
            "query": query,
            "selected_status": selected_status,
            "status_choices": AnthologyTask.Status.choices,
            "hide_completed": hide_completed,
        },
    )


@edit_policy(require_version=True)
@never_cache
@login_required
@require_POST
@team_member_required
def claim_audio_description(request, anthology_id):
    with transaction.atomic():
        book = get_object_or_404(books().select_for_update(), pk=anthology_id)
        person = (
            Person.objects.select_for_update()
            .filter(user=request.user, user__is_active=True, is_active=True, is_external=False)
            .first()
        )
        if person is None:
            raise PermissionDenied("Przejęcie wymaga aktywnego profilu członka zespołu.")
        task = get_object_or_404(
            AnthologyTask.objects.select_for_update(), anthology=book, task_type="audio_description"
        )
        if task.assigned_to_id is not None or task.status != "not_commissioned":
            messages.error(
                request, "Audiodeskrypcja nie jest już wolna. Nie zmieniono przypisania."
            )
            return redirect("core:audio_description_detail", anthology_id=book.pk)
        task.assigned_to = person
        task.status = AnthologyTask.Status.COMMISSIONED
        task.full_clean()
        task.save()
        AudioDescription.objects.get_or_create(anthology=book)
        messages.success(request, "Przejęto audiodeskrypcję.")
    return redirect("core:audio_description_detail", anthology_id=book.pk)


@edit_policy(require_action, require_version=True)
@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def audio_description_detail(request, anthology_id):
    with transaction.atomic():
        book = get_object_or_404(
            books().select_for_update() if request.method == "POST" else books(), pk=anthology_id
        )
        task = get_object_or_404(
            AnthologyTask.objects.select_related("assigned_to"),
            anthology=book,
            task_type="audio_description",
        )
        description = get_object_or_404(AudioDescription, anthology=book)
        # Read before forms bind data: a rejected submission must not lock the page.
        completed = description.stage == AudioDescription.Stage.COMPLETED
        manager = is_coordinator(request.user)
        editable = may_edit(request.user, book)
        writable = may_write(request.user)
        if request.method == "POST":
            require_action(request, book, {})
            if request.POST.get("action", "assignment") == "assignment" and not manager:
                raise PermissionDenied("Przypisanie zmienia koordynator.")
        action = request.POST.get("action", "assignment") if request.method == "POST" else None
        form = DescriptionForm(
            request.POST if action == "assignment" else None,
            instance=task,
            description=description,
        ) if manager else None
        stage_form = StageForm(request.POST if action == "stage" else None, instance=description)
        content_form = ContentForm(
            request.POST if action == "content" else None, instance=description
        )
        note_form = NoteForm(request.POST if action == "note" else None)
        forms_by_action = {
            "assignment": form,
            "stage": stage_form,
            "content": content_form,
            "note": note_form,
        }
        if request.method == "POST":
            chosen = forms_by_action.get(action)
            if chosen is not None and chosen.is_valid():
                if action == "assignment":
                    form.save()
                    description.controllers.set(form.cleaned_data["controllers"])
                elif action == "note":
                    person = get_active_person_profile(request.user)
                    AudioDescriptionNote.objects.create(
                        description=description,
                        author=request.user,
                        author_name=str(person)
                        if person
                        else (request.user.get_full_name() or request.user.get_username()),
                        content=note_form.cleaned_data["note"],
                    )
                elif action == "content":
                    changed = content_form.save(commit=False)
                    changed.save(update_fields=["content"])
                else:
                    changed = stage_form.save(commit=False)
                    changed.save(update_fields=["stage"])
                messages.success(
                    request, "Zapisano audiodeskrypcję." if action != "note" else "Dodano uwagę."
                )
                return redirect("core:audio_description_detail", anthology_id=book.pk)
            if chosen is None:
                messages.error(request, "Nieznana operacja. Odśwież stronę i spróbuj ponownie.")
        return render(
            request,
            "core/audio_description_detail.html",
            {
                "anthology": book,
                "task": task,
                "description": description,
                "stage_form": stage_form,
                "content_form": content_form,
                "note_form": note_form,
                "notes": description.notes.all(),
                "controllers": description.controllers.all() if description else [],
                "form": form,
                "can_edit": editable,
                "can_write": writable,
                "can_manage": manager,
                "is_completed": completed,
            },
            status=400 if request.method == "POST" else 200,
        )
