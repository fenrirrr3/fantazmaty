from django import forms
from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.forms.models import BaseInlineFormSet
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone

from authors.admin import SuperuserOnlyAdminMixin
from authors.models import Author
from people.models import Person
from core.normalization import NormalizedFormMixin, REVIEW_FIELDS, TEXT_FIELDS
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.catalog import IMPORT_ONLY_ROLES, IMPORT_ONLY_STAGE_TYPES

from .models import (
    MAX_REVIEWERS,
    Anthology,
    AnthologyTask,
    Review,
    ReviewAssignment,
    Reviewers,
    Text,
    TextNote,
)


def content_preview(value, limit=100):
    value = " ".join((value or "").split())

    if not value:
        return "–"

    if len(value) <= limit:
        return value

    return f"{value[:limit - 3]}..."


class AnthologyTaskFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parent_was_new = self.instance.pk is None

    def save_new(self, form, commit=True):
        # Anthology.post_save creates the three default tasks before inline save.
        # Apply the submitted values to that task instead of inserting it twice.
        if self.parent_was_new:
            task = AnthologyTask.objects.using(self.instance._state.db).get(
                anthology=self.instance, task_type=form.cleaned_data["task_type"],
            )
            form.instance.pk = task.pk
            form.instance._state.adding = False
            form.instance._state.db = task._state.db
        return super().save_new(form, commit=commit)


class AnthologyTaskInline(admin.TabularInline):
    model = AnthologyTask
    formset = AnthologyTaskFormSet
    extra = 0
    max_num = 3
    can_delete = False

    fields = (
        "task_type",
        "assigned_to",
        "status",
        "commissioned_at",
    )
    readonly_fields = ("commissioned_at",)
    autocomplete_fields = ("assigned_to",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("anthology", "assigned_to")
        )


@admin.register(Anthology)
class AnthologyAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "status",
        "cover_status",
        "cover_author",
        "has_illustrations",
        "print_status",
    )
    list_filter = (
        "status",
        "cover_status",
        "has_illustrations",
        "print_status",
    )
    search_fields = (
        "title__plcontains",
        "cover_author__plcontains",
        "cover_notes__plcontains",
        "production_tasks__assigned_to__first_name__plcontains",
        "production_tasks__assigned_to__last_name__plcontains",
        "production_tasks__assigned_to__email__plcontains",
    )
    fieldsets = (
        (
            "Antologia",
            {
                "fields": (
                    "title",
                    "status",
                    "has_illustrations",
                ),
            },
        ),
        (
            "Okładka",
            {
                "fields": (
                    "cover_status",
                    "cover_author",
                    "cover_notes",
                ),
            },
        ),
        (
            "Druk",
            {"fields": ("print_status",)},
        ),
    )
    ordering = ("title", "pk")
    inlines = (AnthologyTaskInline,)
    list_per_page = 50
    show_full_result_count = False


class WorkflowAssignmentFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for form in self.forms:
            if not form.instance.pk:
                form.instance.workflow_cycle = self.instance.current_workflow_cycle

    def clean(self):
        if self.instance.pk:
            locked_text = Text.objects.select_for_update().get(pk=self.instance.pk)
            if locked_text.current_workflow_cycle != self.instance.current_workflow_cycle:
                raise ValidationError("Przebieg tekstu zmienił się. Odśwież formularz.")
        super().clean()
        from workflow.admin_assignment_rules import protect_assignment
        for form in self.forms:
            if form.cleaned_data:
                protect_assignment(form.instance, form.cleaned_data, deleting=form.cleaned_data.get('DELETE', False))


class WorkflowRoleAssignmentInline(
    SuperuserOnlyAdminMixin,
    admin.TabularInline,
):
    model = WorkflowRoleAssignment
    formset = WorkflowAssignmentFormSet
    extra = 0

    fields = (
        "role", "execution_number", "is_current",
        "assigned_to",
        "assigned_at",
        "notes",
    )
    readonly_fields = ("role", "execution_number", "is_current", "assigned_to", "assigned_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False
    autocomplete_fields = ("assigned_to",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .exclude(role__in=IMPORT_ONLY_ROLES)
            .select_related("text", "assigned_to")
        )


class WorkflowStageInline(
    SuperuserOnlyAdminMixin,
    admin.TabularInline,
):
    model = WorkflowStage
    extra = 0

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(stage_type__in=IMPORT_ONLY_STAGE_TYPES)

    fields = (
        "stage_type",
        "execution_number", "is_current", "is_released",
        "iteration",
        "started_at",
        "ended_at",
        "is_completed",
    )
    ordering = (
        "started_at",
        "stage_type",
        "execution_number", "is_current", "is_released",
        "iteration",
        "pk",
    )

    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class TextAdminForm(NormalizedFormMixin, forms.ModelForm):
    normalization_fields = TEXT_FIELDS

    class Meta:
        model = Text
        fields = "__all__"








@admin.register(Text)
class TextAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = TextAdminForm
    # Admin zawiera powiązania pozwalające ustalić autora zgłoszenia.
    # Koordynatorzy korzystają z widoków aplikacji z odrębnymi uprawnieniami.
    list_display = (
        "title",
        "display_authors",
        "display_author_emails",
        "anthology",
        "length",
        "content_warnings_preview",
    )
    list_filter = ("anthology",)
    search_fields = (
        "title__plcontains",
        "content_warnings__plcontains",
        "authors__first_name__plcontains",
        "authors__last_name__plcontains",
        "authors__pseudonym__plcontains",
        "authors__email__plcontains",
        "anthology__title__plcontains",
    )
    autocomplete_fields = ("authors", "anthology")
    ordering = ("anthology__title", "title", "pk")
    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return fields if request.user.is_superuser else (*fields, "file_url")

    readonly_fields = ("current_workflow_cycle", "import_source", "import_source_row")

    fieldsets = (
        (
            "Tekst",
            {
                "fields": (
                    "title",
                    "authors",
                    "anthology",
                    "length",
                    "content_warnings",
                    "coordinator_note",
                    "file_url",
                ),
            },
        ),
        (
            "Workflow",
            {"fields": ("current_workflow_cycle", "import_source", "import_source_row")},
        ),
    )
    inlines = (
        WorkflowRoleAssignmentInline,
        WorkflowStageInline,
    )
    list_per_page = 50
    show_full_result_count = False

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change:
            text = form.instance
            stages = WorkflowStage.objects.using(text._state.db).filter(
                text_id=text.pk,
                workflow_cycle=text.current_workflow_cycle,
            )
            if not stages.exists():
                stages.create(
                    text=text,
                    workflow_cycle=text.current_workflow_cycle,
                    stage_type=WorkflowStage.StageType.READY_FOR_EDITING,
                    iteration=1,
                )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("anthology")
            .prefetch_related("authors")
        )

    @admin.display(description="autorzy")
    def display_authors(self, obj):
        return ", ".join(
            str(author) for author in obj.authors.all()
        ) or "–"

    @admin.display(description="adresy e-mail")
    def display_author_emails(self, obj):
        return ", ".join(
            author.email for author in obj.authors.all() if author.email
        ) or "–"

    @admin.display(description="trigger warningi")
    def content_warnings_preview(self, obj):
        return content_preview(obj.content_warnings, limit=80)


@admin.register(TextNote)
class TextNoteAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "text",
        "author",
        "is_important",
        "created_at",
        "note_preview",
    )
    list_filter = (
        "is_important",
        "created_at",
        "text__anthology",
    )
    search_fields = (
        "text__title__plcontains",
        "text__authors__first_name__plcontains",
        "text__authors__last_name__plcontains",
        "text__authors__pseudonym__plcontains",
        "author__first_name__plcontains",
        "author__last_name__plcontains",
        "content__plcontains",
    )
    fields = (
        "text",
        "content",
        "is_important",
        "author",
        "created_at",
    )
    autocomplete_fields = ("text",)
    readonly_fields = ("author", "created_at")
    ordering = ("-created_at", "-pk")
    list_select_related = ("text", "author")
    list_per_page = 50
    show_full_result_count = False

    def save_model(self, request, obj, form, change):
        if not self.has_superuser_access(request):
            raise PermissionDenied

        if obj._state.adding:
            obj.author = request.user

        super().save_model(request, obj, form, change)

    @admin.display(description="treść")
    def note_preview(self, obj):
        return content_preview(obj.content)


class ReviewAdminForm(NormalizedFormMixin, forms.ModelForm):
    normalization_fields = REVIEW_FIELDS
    confirm_submission_warnings = forms.BooleanField(
        label="Zapoznałem się z ostrzeżeniami i potwierdzam zapis",
        required=False,
        help_text=(
            "Dotyczy ostrzeżenia o czarnej liście autora "
            "lub możliwym duplikacie zgłoszenia."
        ),
    )
    submission_warnings_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
    )

    WARNING_TOKEN_SALT = "texts.admin.review-submission-warnings"

    class Meta:
        model = Review
        fields = (
            "author",
            "coauthors",
            "author_first_name",
            "author_last_name",
            "title",
            "genre",
            "length",
            "content_warnings",
            "email",
            "phone_number",
            "anthology",
            "old_reviews",
        "is_hidden",
            "status",
            "author_notified_at",
            "decision_at",
            "copied_text",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.submission_warnings = []
        if self.instance._state.adding and "anthology" in self.fields:
            self.fields["anthology"].queryset = Anthology.objects.filter(status=Anthology.Status.IN_PREPARATION).order_by("title", "pk")

        # Dane zostaną uzupełnione po stronie serwera po wyborze autora.
        # Bez powiązanego autora pozostają wymagane w clean().
        for name in ("author_first_name", "author_last_name", "email"):
            if name in self.fields:
                self.fields[name].required = False

    def clean(self):
        cleaned_data = super().clean()
        if "old_reviews" not in self.fields:
            cleaned_data["old_reviews"] = self.instance.old_reviews
        author = cleaned_data.get("author")

        if author is not None:
            cleaned_data["author_first_name"] = author.first_name
            cleaned_data["author_last_name"] = author.last_name
            cleaned_data["email"] = author.email or ""
            if not author.email and not cleaned_data.get("old_reviews"):
                self.add_error("author", "Uzupełnij adres e-mail autora historycznego przed dodaniem nowego zgłoszenia.")
        else:
            for name in ("author_first_name", "author_last_name", "email"):
                if name in self.fields and not cleaned_data.get(name) and not (name == "email" and cleaned_data.get("old_reviews")):
                    self.add_error(
                        name,
                        "Uzupełnij dane albo wybierz autora z bazy.",
                    )

        if self.errors:
            return cleaned_data

        identity_fields = {
            "author",
            "author_first_name",
            "author_last_name",
            "title",
            "email",
            "anthology",
        }

        check_warnings = (
            self.instance._state.adding
            or bool(identity_fields.intersection(self.changed_data))
        )

        if not check_warnings:
            return cleaned_data

        # Wspólna funkcja dla admina i importu, implementowana
        # w texts/services.py. Zwraca listę komunikatów tekstowych
        # i uwzględnia także recenzje oznaczone old_reviews.
        from .services import get_review_submission_warnings

        self.submission_warnings = get_review_submission_warnings(
            author=author,
            author_first_name=cleaned_data["author_first_name"],
            author_last_name=cleaned_data["author_last_name"],
            email=cleaned_data["email"],
            title=cleaned_data["title"],
            exclude_review_id=self.instance.pk,
            anthology_id=getattr(cleaned_data.get("anthology"), "pk", None),
        )

        if not self.submission_warnings:
            return cleaned_data

        payload = {
            "review_id": self.instance.pk,
            "author_id": author.pk if author is not None else None,
            "first_name": cleaned_data["author_first_name"],
            "last_name": cleaned_data["author_last_name"],
            "email": cleaned_data["email"],
            "title": cleaned_data["title"],
            "warnings": list(self.submission_warnings),
        }

        previous_payload = None
        token = cleaned_data.get("submission_warnings_token", "")

        if token:
            try:
                previous_payload = signing.loads(
                    token,
                    salt=self.WARNING_TOKEN_SALT,
                    max_age=3600,
                )
            except signing.BadSignature:
                pass

        confirmed = (
            cleaned_data.get("confirm_submission_warnings")
            and previous_payload == payload
        )

        if not confirmed:
            # Podpis wiąże potwierdzenie z uprzednio pokazanym
            # ostrzeżeniem oraz konkretnymi danymi zgłoszenia.
            token = signing.dumps(
                payload,
                salt=self.WARNING_TOKEN_SALT,
                compress=True,
            )
            self.data = self.data.copy()
            self.data[
                self.add_prefix("submission_warnings_token")
            ] = token
            self.data[
                self.add_prefix("confirm_submission_warnings")
            ] = ""

            self.add_error(
                "confirm_submission_warnings",
                " ".join(self.submission_warnings)
                + " Sprawdź zgłoszenie, zaznacz potwierdzenie "
                "i ponownie zapisz formularz.",
            )

        return cleaned_data


class ReviewAssignmentAdminForm(forms.ModelForm):
    archive_assigned_at = forms.DateTimeField(label="Data przydzielenia", required=False)
    archive_opinion_changed_at = forms.DateField(label="Data opinii", required=False)

    class Meta:
        model = ReviewAssignment
        fields = (
            "position",
            "user",
            "historical_person",
            "opinion",
            "notes",
        )

    def clean_user(self):
        user = self.cleaned_data.get("user")
        if self.instance.review_id and self.instance.review.old_reviews:
            return user

        if user is None:
            if self.instance._state.adding:
                raise forms.ValidationError(
                    "Wybierz recenzenta dla nowego przydziału."
                )

            if self.instance.user_id is not None:
                raise forms.ValidationError(
                    "Aby zwolnić miejsce, usuń przydział. "
                    "Nie usuwaj samego powiązania z recenzentem."
                )

            # Historyczna ocena po usunięciu konta.
            return None

        user_changed = (
            self.instance._state.adding
            or self.instance.user_id != user.pk
        )

        if user_changed:
            if not user.is_active:
                raise forms.ValidationError(
                    "Wybrane konto użytkownika jest nieaktywne."
                )

            if not Person.objects.active().filter(user_id=user.pk).exists():
                raise forms.ValidationError(
                    "Recenzent musi należeć do aktywnego zespołu."
                )

        if (
            not self.instance._state.adding
            and user_changed
            and (
                self.instance.opinion != ReviewAssignment.Opinion.READING
                or self.instance.notes.strip()
            )
        ):
            raise forms.ValidationError(
                "Nie można przypisać istniejącej opinii lub uwag "
                "innemu recenzentowi. Najpierw rozstrzygnij los "
                "dotychczasowego przydziału."
            )

        return user


class ReviewAssignmentInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()

        if any(self.errors):
            return

        positions = set()
        users = set()
        count = 0

        for form in self.forms:
            data = getattr(form, "cleaned_data", None)

            if not data or data.get("DELETE"):
                continue

            position = data.get("position")

            if position is None:
                continue

            count += 1
            user = data.get("user")

            if position in positions:
                raise ValidationError(
                    "Dwa przydziały nie mogą zajmować tego samego miejsca."
                )

            positions.add(position)

            if user is not None:
                if user.pk in users:
                    raise ValidationError(
                        "Ta sama osoba nie może mieć dwóch przydziałów "
                        "w jednej recenzji."
                    )

                users.add(user.pk)

        if count > MAX_REVIEWERS:
            raise ValidationError(
                f"Recenzja może mieć najwyżej {MAX_REVIEWERS} recenzentów."
            )


class ArchivedReviewInlineMixin(SuperuserOnlyAdminMixin):
    """Archive can be corrected only in the superuser administration panel."""
    pass


class ReviewersInline(ArchivedReviewInlineMixin, admin.StackedInline):
    model = Reviewers
    extra = 0
    max_num = 1
    can_delete = False

    fields = ("general_notes",)

    def get_extra(self, request, obj=None, **kwargs):
        if obj is None:
            return 1

        if obj.old_reviews:
            return 0

        return 0 if Reviewers.objects.filter(review=obj).exists() else 1


class ReviewAssignmentInline(
    ArchivedReviewInlineMixin,
    admin.TabularInline,
):
    model = ReviewAssignment
    form = ReviewAssignmentAdminForm
    formset = ReviewAssignmentInlineFormSet

    extra = 0
    max_num = MAX_REVIEWERS

    fields = (
        "position",
        "user",
        "historical_person",
        "opinion",
        "notes",
        "assigned_at",
        "opinion_changed_at",
    )
    readonly_fields = (
        "assigned_at",
        "opinion_changed_at",
    )
    autocomplete_fields = ("user",)
    ordering = ("position", "pk")

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj and obj.old_reviews and request.user.is_superuser:
            return tuple("archive_" + name if name in self.readonly_fields else name for name in fields)
        return fields

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.old_reviews and request.user.is_superuser:
            return ()
        return (*self.readonly_fields, "position", "user", "historical_person", "opinion")

    def has_add_permission(self, request, obj=None):
        return bool(obj and obj.old_reviews and request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return bool(obj and obj.old_reviews and request.user.is_superuser)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("review", "user")
        )

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj and obj.old_reviews:
            base_form = formset.form
            class HistoricalAssignmentForm(base_form):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    for name in ("assigned_at", "opinion_changed_at"):
                        self.initial["archive_" + name] = getattr(self.instance, name) if self.instance.pk else None

                def clean_user(self):
                    return self.cleaned_data.get("user")

                def clean(self):
                    data = super().clean()
                    from texts.archive_dates import validate_archive_dates
                    try:
                        validate_archive_dates(data.get("archive_assigned_at"), data.get("archive_opinion_changed_at"))
                    except ValidationError as error:
                        for name, messages in error.message_dict.items():
                            self.add_error("archive_" + name, messages)
                    # Model.clean must validate the new dates, not the stale instance values.
                    self.instance.assigned_at = data.get("archive_assigned_at")
                    self.instance.opinion_changed_at = data.get("archive_opinion_changed_at")
                    if self.instance._state.adding and not data.get("user") and not data.get("historical_person") and not data.get("DELETE"):
                        raise forms.ValidationError("Wybierz konto lub historyczny profil recenzenta.")
                    return data

                def save(self, commit=True):
                    self.instance.assigned_at = self.cleaned_data.get("archive_assigned_at")
                    self.instance.opinion_changed_at = self.cleaned_data.get("archive_opinion_changed_at")
                    return super().save(commit=commit)
            formset.form = HistoricalAssignmentForm
        return formset



@admin.register(Review)
class ReviewAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    # Pełna obsługa zgłoszenia w adminie wymaga dostępu do autora.
    # Koordynatorzy i recenzenci korzystają z widoków aplikacji.
    form = ReviewAdminForm

    list_display = (
        "title",
        "display_author",
        "anthology",
        "genre",
        "length",
        "status",
        "old_reviews",
        "is_hidden",
        "created_at",
        "decision_at",
        "author_notified_at",
        "display_copied_to_text",
    )
    list_filter = (
        "old_reviews",
        "is_hidden",
        "status",
        "anthology",
        "genre",
        "created_at",
        "decision_at",
        "author_notified_at",
    )
    search_fields = (
        "title__plcontains",
        "author_first_name__plcontains",
        "author_last_name__plcontains",
        "email__plcontains",
        "phone_number__plcontains",
        "content_warnings__plcontains",
        "author__first_name__plcontains",
        "author__last_name__plcontains",
        "author__pseudonym__plcontains",
        "author__email__plcontains",
        "anthology__title__plcontains",
        "copied_text__title__plcontains",
    )
    autocomplete_fields = (
        "author",
        "coauthors",
        "anthology",
        "copied_text",
    )
    readonly_fields = (
        "created_at",
        "decision_at",
    )

    fieldsets = (
        (
            "Zgłoszenie",
            {
                "fields": (
                    "title",
                    "anthology",
                    "genre",
                    "length",
                    "content_warnings",
                    "old_reviews",
        "is_hidden",
                    "created_at",
                ),
            },
        ),
        (
            "Autor – wyłącznie superuser",
            {
                "fields": (
                    "author",
                    "coauthors",
                    "author_first_name",
                    "author_last_name",
                    "email",
                    "phone_number",
                ),
            },
        ),
        (
            "Kontrola zgłoszenia",
            {
                "fields": (
                    "confirm_submission_warnings",
                    "submission_warnings_token",
                ),
            },
        ),
        (
            "Decyzja",
            {
                "fields": (
                    "status",
                    "decision_at",
                    "author_notified_at",
                ),
            },
        ),
        (
            "Proces wydawniczy",
            {"fields": ("copied_text",)},
        ),
    )

    ordering = ("-created_at", "-pk")
    date_hierarchy = "created_at"
    inlines = (ReviewersInline, ReviewAssignmentInline)
    list_per_page = 50
    show_full_result_count = False

    class Media:
        js = ("texts/admin/review_author_autofill.js",)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.old_reviews:
            return ("created_at", "old_reviews")
        return (*super().get_readonly_fields(request, obj), "status", "copied_text", "author_notified_at", *(("old_reviews",) if obj else ()))

    def get_urls(self):
        return [
            path(
                "author-details/<int:author_id>/",
                self.admin_site.admin_view(self.author_details_view),
                name="texts_review_author_details",
            ),
        ] + super().get_urls()

    def author_details_view(self, request, author_id):
        if not self.has_superuser_access(request):
            raise PermissionDenied

        author = get_object_or_404(Author, pk=author_id)

        response = JsonResponse(
            {
                "first_name": author.first_name,
                "last_name": author.last_name,
                "email": author.email,
                "is_blacklisted": author.is_blacklisted,
                "warning": (
                    "Autor znajduje się na czarnej liście."
                    if author.is_blacklisted
                    else ""
                ),
            }
        )
        response["Cache-Control"] = "no-store, private"
        return response

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(
            db_field,
            request,
            **kwargs,
        )

        if db_field.name == "author" and formfield is not None:
            formfield.widget.attrs["data-author-details-url"] = reverse(
                f"{self.admin_site.name}:texts_review_author_details",
                args=(0,),
            )

        return formfield

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("author", "anthology", "copied_text").prefetch_related("coauthors")
        )

    def changeform_view(
        self,
        request,
        object_id=None,
        form_url="",
        extra_context=None,
    ):
        if request.method != "POST" or not self.has_superuser_access(request):
            return super().changeform_view(
                request,
                object_id,
                form_url,
                extra_context,
            )

        using = router.db_for_write(Review)

        # Zapis recenzji i wszystkich przydziałów jest atomowy.
        # Serwisy przydzielania muszą blokować ten sam rekord Review.
        with transaction.atomic(using=using):
            if object_id is not None:
                obj = self.get_object(request, unquote(object_id))

                if obj is not None:
                    (
                        Review.objects.using(using)
                        .select_for_update()
                        .get(pk=obj.pk)
                    )

            return super().changeform_view(
                request,
                object_id,
                form_url,
                extra_context,
            )

    def save_model(self, request, obj, form, change):
        if not self.has_superuser_access(request):
            raise PermissionDenied

        if obj.author_id is not None:
            obj.author_first_name = obj.author.first_name
            obj.author_last_name = obj.author.last_name
            obj.email = obj.author.email or ""

        decision_statuses = {
            Review.Status.ACCEPTED,
            Review.Status.REJECTED,
            Review.Status.WITHDRAWN,
        }

        if not obj.old_reviews and (not change or "status" in form.changed_data):
            obj.decision_at = (
                timezone.localdate()
                if obj.status in decision_statuses
                else None
            )

        if not change:
            from texts.blacklist import apply_blacklist
            apply_blacklist(obj)
        super().save_model(request, obj, form, change)

        if obj.copied_text_id is not None:
            Text.objects.using(obj._state.db).filter(
                pk=obj.copied_text_id
            ).update(content_warnings=obj.content_warnings)
            from core.edit_versions import bump
            bump('texts.text', obj.copied_text_id, obj._state.db)

    @admin.display(
        description="autor",
        ordering="author_last_name",
    )
    def display_author(self, obj):
        return obj.author_display_name

    @admin.display(
        description="dodany do tekstów",
        boolean=True,
        ordering="copied_text",
    )
    def display_copied_to_text(self, obj):
        return obj.is_copied_to_text


# Reviewers i ReviewAssignment są edytowane wyłącznie jako inline Review.
# Nie rejestrujemy osobnych adminów omijających blokadę rekordu recenzji.
