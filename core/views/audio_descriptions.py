from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from core.edit_policy import edit_policy
from core.models import AudioDescription, EditRevision
from core.pagination import paginate_items
from core.permissions import team_member_required, get_active_person_profile, is_coordinator
from core.table_sorting import DisplayTable
from people.models import Person
from texts.models import Anthology, AnthologyTask


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


class DescriptionForm(forms.ModelForm):
    controllers = forms.ModelMultipleChoiceField(
        label="Osoby do kontroli", queryset=Person.objects.none(), required=False
    )

    class Meta:
        model = AnthologyTask
        fields = ("assigned_to", "status")
        labels = {"assigned_to": "Osoba wykonująca audiodeskrypcję"}

    def __init__(self, *args, description=None, manager=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.description = description
        self.manager = manager
        if not manager:
            self.fields.pop("assigned_to")
            self.fields.pop("controllers")
        else:
            current = (
                list(description.controllers.values_list("pk", flat=True)) if description else []
            )
            active = Person.objects.active().filter(user__is_active=True).values("pk")
            self.fields["controllers"].queryset = Person.objects.filter(
                Q(pk__in=active) | Q(pk__in=current)
            )
            self.fields["controllers"].initial = current
            self.fields["assigned_to"].queryset = Person.objects.filter(
                Q(pk__in=active) | Q(pk=self.instance.assigned_to_id)
            )
            self.fields["assigned_to"].widget.attrs["data-searchable-person"] = "true"
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_status(self):
        status = self.cleaned_data["status"]
        if not self.manager and not self.instance.assigned_to_id and status != "not_commissioned":
            raise forms.ValidationError(
                "Najpierw koordynator musi przypisać wykonawcę audiodeskrypcji."
            )
        return status

    def clean(self):
        values = super().clean()
        if not self.manager and values.get("status") == AnthologyTask.Status.NOT_COMMISSIONED:
            raise forms.ValidationError("Zmianę przypisania wykonuje koordynator.")
        return values


@never_cache
@login_required
@require_GET
@team_member_required
def audio_descriptions(request):
    query = request.GET.get("q", "").strip()[:200]
    selected_status = request.GET.get("status", "")
    tasks = (
        AnthologyTask.objects.filter(task_type="audio_description", anthology__in=books())
        .select_related("anthology", "assigned_to", "anthology__audio_description")
        .prefetch_related("anthology__audio_description__controllers")
    )
    if selected_status:
        tasks = tasks.filter(status=selected_status)
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
                "Osoba": ("person", lambda row: str(row["task"].assigned_to or "")),
                "Status": ("status", lambda row: row["task"].get_status_display()),
                "Kontrola": (
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


@edit_policy(require_edit, require_version=True)
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
        description = AudioDescription.objects.filter(anthology=book).first()
        manager = is_coordinator(request.user)
        editable = may_edit(request.user, book)
        if request.method == "POST":
            require_edit(request, book, {})
        form = DescriptionForm(
            request.POST if request.method == "POST" else None,
            instance=task,
            description=description,
            manager=manager,
        )
        if request.method == "POST" and form.is_valid():
            form.save()
            if manager:
                description, _ = AudioDescription.objects.get_or_create(anthology=book)
                description.controllers.set(form.cleaned_data["controllers"])
            messages.success(request, "Zapisano audiodeskrypcję.")
            return redirect("core:audio_description_detail", anthology_id=book.pk)
        return render(
            request,
            "core/audio_description_detail.html",
            {
                "anthology": book,
                "task": task,
                "controllers": description.controllers.all() if description else [],
                "form": form,
                "can_edit": editable,
                "can_manage": manager,
            },
            status=400 if request.method == "POST" else 200,
        )
