from django import forms
from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.forms.models import BaseInlineFormSet
from django.http import JsonResponse, HttpResponseBadRequest
import secrets
import time
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone

from authors.admin import SuperuserOnlyAdminMixin
from authors.models import Author
from people.models import Person
from core.normalization import NormalizedFormMixin, TEXT_FIELDS
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES

from .models import (
    MAX_REVIEWERS,
    Anthology,
    AnthologyTask,
    ExtractVolume, ExtractVolumeCredit, ExtractTextLink,
    Review,
    ReviewAssignment,
    Reviewers,
    Text,
    TextNote,
    TextTranslation,
    ForeignAuthor,
    Translator,
)
from workflow.admin_performer_forms import WorkflowPerformerForm, WorkflowPerformerFormSet
from core.review_submission_forms import ReviewAdminForm


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
    max_num = 4
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


class ExtractVolumeCreditInline(admin.TabularInline):
    model = ExtractVolumeCredit
    extra = 0
    autocomplete_fields = ('person',)
    fields = ('person', 'role', 'source_name', 'position')


@admin.register(Anthology)
class AnthologyAdmin(admin.ModelAdmin):
    def get_inlines(self, request, obj=None):
        inlines = list(super().get_inlines(request, obj))
        if obj and ExtractVolume.objects.filter(anthology=obj).exists():
            inlines.append(ExtractVolumeCreditInline)
        return inlines

    def get_deleted_objects(self, objs, request):
        # Only the parent deletion may remove its untouched default tasks.
        previous = getattr(request, '_deleting_anthologies', None)
        request._deleting_anthologies = {obj.pk for obj in objs}
        try:
            return super().get_deleted_objects(objs, request)
        finally:
            if previous is None:
                del request._deleting_anthologies
            else:
                request._deleting_anthologies = previous

    def _delete_anthologies(self, request, queryset):
        using = queryset.db
        with transaction.atomic(using=using):
            books = list(queryset.select_for_update().order_by('pk'))
            list(AnthologyTask.objects.using(using).select_for_update().filter(
                anthology_id__in=[book.pk for book in books]).order_by('pk'))
            _, _, permissions, protected = self.get_deleted_objects(books, request)
            if permissions or protected:
                raise PermissionDenied('Antologia ma zadania z przypisaniami lub inne chronione dane. Odśwież podgląd usuwania.')
            queryset.delete()

    def delete_model(self, request, obj):
        self._delete_anthologies(request, Anthology.objects.using(obj._state.db).filter(pk=obj.pk))

    def delete_queryset(self, request, queryset):
        self._delete_anthologies(request, queryset)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj and obj.is_novel:
            # Keep translation visible only to allow correction of old invalid records.
            hidden = {'has_illustrations'} | (set() if obj.is_translated else {'is_translated'})
            return tuple((name, {**options, 'fields': tuple(f for f in options['fields'] if f not in hidden)})
                         for name, options in fieldsets)
        return fieldsets

    def get_search_results(self, request, queryset, search_term):
        queryset, duplicates = super().get_search_results(request, queryset, search_term)
        if request.GET.get("app_label") == "texts" and request.GET.get("model_name") == "review" and request.GET.get("field_name") == "anthology":
            queryset = queryset.filter(status=Anthology.Status.IN_PREPARATION, is_novel=False)
        return queryset, duplicates

    list_display = (
        "title",
        "is_novel",
        "status",
        "cover_status",
        "cover_author",
        "has_illustrations",
        "is_translated",
        "print_status",
    )
    list_filter = (
        "status",
        "is_novel",
        "cover_status",
        "has_illustrations",
        "is_translated",
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
                    "is_novel",
                    "status",
                    "has_illustrations",
        "is_translated",
                ),
            },
        ),
        (
            "Okładka",
            {
                "classes": ("cms-after-inlines",),
                "fields": (
                    "cover_status",
                    "cover_author",
                    "cover_notes",
                ),
            },
        ),
        (
            "Druk",
            {"classes": ("cms-after-inlines",), "fields": ("print_status",)},
        ),
    )
    ordering = ("title", "pk")
    inlines = (AnthologyTaskInline,)
    list_per_page = 50
    show_full_result_count = False




class WorkflowStageInline(SuperuserOnlyAdminMixin, admin.TabularInline):
    model = WorkflowStage
    form = WorkflowPerformerForm
    formset = WorkflowPerformerFormSet
    verbose_name_plural = "Workflow – wykonawcy etapów"
    extra = 0
    can_delete = False
    fields = ("stage_label", "performer", "started_at", "ended_at", "is_completed", "is_current", "edit_dates_link", "reopen_stage_link", "delete_stage_link", "workflow_version")
    readonly_fields = ("stage_label", "started_at", "ended_at", "is_completed", "is_current", "edit_dates_link", "reopen_stage_link", "delete_stage_link")
    template = "admin/texts/text/workflow_inline.html"

    def get_queryset(self, request):
        from django.db.models import Case, When, IntegerField, Value
        from workflow.catalog import active_stage_choices
        order = [kind for kind, _ in active_stage_choices()]
        return (super().get_queryset(request).exclude(stage_type__in=IMPORT_ONLY_STAGE_TYPES)
                .select_related('text','assignment__assigned_to')
                .annotate(_workflow_order=Case(*[When(stage_type=kind,then=Value(i)) for i,kind in enumerate(order)],default=Value(999),output_field=IntegerField()))
                .order_by('-workflow_cycle','_workflow_order','execution_number','iteration','pk'))

    @admin.display(description="Etap")
    def stage_label(self, obj):
        from workflow.labels import stage_label
        return stage_label(obj)

    @admin.display(description="Daty i przekazanie")
    def edit_dates_link(self, obj):
        from django.utils.html import format_html
        return format_html('<a href="{}">Ustaw daty / zakończ etap</a>', reverse('admin:workflow_stage_dates', args=[obj.pk]))

    @admin.display(description="Usuwanie")
    def delete_stage_link(self, obj):
        from django.utils.html import format_html
        return format_html('<a href="{}?action=delete">Usuń etap</a>', reverse('admin:workflow_stage_correct', args=[obj.pk]))

    @admin.display(description="Zakończenie")
    def reopen_stage_link(self, obj):
        from workflow.reservation_repair import reservation_candidate
        if obj.workflow_cycle == obj.text.current_workflow_cycle and reservation_candidate(obj):
            from django.utils.html import format_html
            return format_html('<a href="{}?action=restore_reservation">Przywróć oczekiwanie na przekazanie</a>', reverse('admin:workflow_stage_correct', args=[obj.pk]))
        if (not obj.is_completed or not obj.is_current or not obj.is_released or obj.is_skipped
                or obj.workflow_cycle != obj.text.current_workflow_cycle
                or obj.stage_type in ('ready', 'withdrawn')):
            return '–'
        from django.utils.html import format_html
        return format_html('<a href="{}?action=reopen">Cofnij zakończenie</a>', reverse('admin:workflow_stage_correct', args=[obj.pk]))

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class TextNoteInline(SuperuserOnlyAdminMixin, admin.StackedInline):
    model = TextNote
    fields = ("content", "is_important", "author", "created_at")
    readonly_fields = ("author", "created_at")
    extra = 0
    verbose_name_plural = "Notatki do tekstu"


def review_text_initial(review):
    """Only persisted review data is authoritative for the publication popup."""
    authors = list(review.coauthors.values_list('pk', flat=True))
    if review.author_id:
        authors.append(review.author_id)
    elif review.email:
        matches = list(Author.objects.filter(email__iexact=review.email).values_list('pk', flat=True)[:2])
        if len(matches) == 1:
            authors.append(matches[0])
    return {'title': review.title, 'genre': review.genre, 'length': review.length, 'content_warnings': review.content_warnings, 'file_url': review.file_url,
            'anthology': review.anthology_id, 'authors': sorted(set(authors)),
            'source_author_first_name': review.author_first_name, 'source_author_last_name': review.author_last_name,
            'source_author_email': review.email, 'source_author_pseudonym': review.author_pseudonym}


def review_source_signature(review):
    # Phone and contract confirmations also affect publication preparation.
    return {**review_text_initial(review), 'author': review.author_id, 'phone': review.phone_number,
            'status': review.status, 'detached': review.publication_detached}


class TextAdminForm(NormalizedFormMixin, forms.ModelForm):
    normalization_fields = TEXT_FIELDS
    source_author_first_name = forms.CharField(required=False, widget=forms.HiddenInput)
    source_author_last_name = forms.CharField(required=False, widget=forms.HiddenInput)
    source_author_email = forms.EmailField(required=False, widget=forms.HiddenInput)
    source_author_pseudonym = forms.CharField(required=False, widget=forms.HiddenInput)
    source_contract_received = forms.BooleanField(label="Potwierdzam otrzymanie umowy autora", required=False)
    source_coauthor_contracts = forms.ModelMultipleChoiceField(label="Potwierdzam umowy wskazanych współautorów", queryset=Author.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    source_update_author_phone = forms.BooleanField(label="Uaktualnij telefon autora danymi zgłoszenia", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        source = getattr(self, 'source_review', None)
        anthology_id = self.data.get('anthology') if self.is_bound else self.initial.get('anthology')
        translated = (self.instance.anthology_id and self.instance.anthology.is_translated) or (
            str(anthology_id or '').isdecimal() and Anthology.objects.filter(pk=anthology_id, is_translated=True).exists())
        if translated and 'authors' in self.fields:
            self.fields['authors'].required = False
        novel = (self.instance.anthology_id and self.instance.anthology.is_novel) or (
            str(anthology_id or '').isdecimal() and Anthology.objects.filter(pk=anthology_id, is_novel=True).exists())
        if 'length' in self.fields:
            self.fields['length'].required = not novel and self.instance.import_source != 'extracts-v1'
        if novel:
            for name in ('title', 'file_url', 'content_warnings'):
                if name in self.fields:
                    self.fields[name].required = False
                    self.fields[name].disabled = True
                    self.fields[name].widget = forms.HiddenInput()
        if novel and 'authors' in self.fields:
            self.fields['authors'].required = False
            self.fields['authors'].help_text = 'Autorzy zostaną pobrani z danych całej powieści.'
        if source is not None:
            canonical = review_text_initial(source)
            # These fields describe the saved source, not another editable author form.
            for name in ('authors', 'source_author_first_name', 'source_author_last_name',
                         'source_author_email', 'source_author_pseudonym'):
                if name in self.fields:
                    self.fields[name].disabled = True
                    self.initial[name] = canonical[name]
            self.fields['authors'].required = False
            self.fields['authors'].help_text = 'Autorzy zapisanej recenzji. Aby ich zmienić, zapisz zmiany w recenzji i ponownie użyj +.'
        elif self.initial.get('source_author_email') and 'authors' in self.fields:
            self.fields['authors'].required = False

    def clean(self):
        data = super().clean()
        source = getattr(self, 'source_review', None)
        anthology_id = self.data.get('anthology') if self.is_bound else self.initial.get('anthology')
        translated = (self.instance.anthology_id and self.instance.anthology.is_translated) or (
            str(anthology_id or '').isdecimal() and Anthology.objects.filter(pk=anthology_id, is_translated=True).exists())
        if translated and 'authors' in self.fields:
            self.fields['authors'].required = False
        if source is not None:
            from core.services.reviews import resolve_publication_authors
            try:
                resolve_publication_authors(source,
                    contract_received=data.get('source_contract_received', False),
                    confirmed_coauthor_ids=[a.pk for a in data.get('source_coauthor_contracts', [])],
                    update_author_phone=data.get('source_update_author_phone', False), persist=False)
            except ValidationError as error:
                self.add_error(None, ' '.join(error.messages))
            if data.get('anthology') and data['anthology'].pk != source.anthology_id:
                self.add_error('anthology', 'Tekst musi należeć do antologii zapisanej recenzji.')
            if getattr(self, 'source_error', None):
                self.add_error(None, self.source_error)
        elif (data.get('anthology') and data['anthology'].is_novel) or (self.instance.anthology_id and self.instance.anthology.is_novel):
            from texts.models import NovelProfile
            book = data.get('anthology') or self.instance.anthology
            if not NovelProfile.objects.filter(anthology=book, authors__isnull=False).exists():
                self.add_error(None, 'Najpierw przypisz autorów w panelu admina, w sekcji „Dane powieści”.')
        elif data.get('anthology') and data['anthology'].is_translated:
            if data.get('authors'):
                self.add_error('authors', 'Autora zagranicznego dodaj w sekcji tłumaczenia, po zapisaniu tekstu.')
        elif not data.get('authors'):
            self.add_error('authors', 'Wybierz autora.')
        return data

    class Meta:
        model = Text
        fields = "__all__"








@admin.register(ForeignAuthor)
class ForeignAuthorAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'pseudonym', 'email')
    search_fields = ('first_name__plcontains', 'last_name__plcontains', 'pseudonym__plcontains', 'email__plcontains')
    fields = ('first_name', 'last_name', 'pseudonym', 'email', 'phone_number', 'notes')


@admin.register(Translator)
class TranslatorAdmin(ForeignAuthorAdmin):
    list_display = (*ForeignAuthorAdmin.list_display, 'language')
    search_fields = (*ForeignAuthorAdmin.search_fields, 'language__plcontains')
    fields = (*ForeignAuthorAdmin.fields, 'language')


class TextTranslationInline(admin.StackedInline):
    model = TextTranslation
    fields = ('foreign_authors', 'translators', 'original_verifier')
    autocomplete_fields = ('foreign_authors', 'translators')
    extra = 0
    max_num = 1
    can_delete = False


@admin.register(TextTranslation)
class TextTranslationAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('text', 'display_foreign_authors', 'display_translators')
    list_filter = ('text__anthology', 'text__anthology__is_translated')
    search_fields = ('text__title__plcontains', 'text__anthology__title__plcontains',
                     'translators__first_name__plcontains', 'translators__last_name__plcontains',
                     'foreign_authors__first_name__plcontains', 'foreign_authors__last_name__plcontains')
    autocomplete_fields = ('text', 'foreign_authors', 'translators')
    readonly_fields = ('text',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).filter(text__anthology__is_translated=True).select_related('text__anthology').prefetch_related('translators', 'foreign_authors')

    @admin.display(description='Autor zagraniczny')
    def display_foreign_authors(self, obj):
        return ', '.join(str(a) for a in obj.foreign_authors.all()) or '–'

    @admin.display(description='Tłumacz')
    def display_translators(self, obj):
        return ', '.join(str(a) for a in obj.translators.all()) or '–'


@admin.register(Text)
class TextAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    def repeat_form(self, request, object_id):
        from django.shortcuts import render
        from django.http import HttpResponseNotAllowed
        from core.forms import RestartWorkflowForm
        if request.method != 'GET':
            return HttpResponseNotAllowed(['GET'])
        obj = get_object_or_404(Text, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        return render(request, 'admin/texts/repeat_form.html', {
            **self.admin_site.each_context(request), 'title': 'Powtórz etapy: ' + obj.title,
            'text': obj, 'form': RestartWorkflowForm(text=obj), 'opts': self.model._meta,
        })

    @admin.display(description='Powtórzenie etapów')
    def repeat_workflow_link(self, obj):
        if not obj or not obj.pk:
            return 'Zapisz tekst, aby zarządzać etapami.'
        from django.utils.html import format_html
        from django.urls import reverse
        return format_html('<a href="{}">Wybierz etapy i przygotuj podgląd powtórzenia</a>', reverse('admin:texts_text_repeat', args=[obj.pk]))

    def get_search_results(self, request, queryset, search_term):
        queryset, duplicates = super().get_search_results(request, queryset, search_term)
        if request.GET.get("app_label") == "texts" and request.GET.get("model_name") == "review" and request.GET.get("field_name") == "copied_text":
            queryset = queryset.filter(workflow_stages__isnull=False).distinct()
        if (request.GET.get('app_label'), request.GET.get('model_name'), request.GET.get('field_name')) == ('illustrations', 'illustration', 'text'):
            from illustrations.services import illustration_texts_queryset
            queryset = queryset.filter(pk__in=illustration_texts_queryset().values('pk'))
        return queryset, duplicates

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        for key in ('source_author_first_name', 'source_author_last_name', 'source_author_email', 'source_author_pseudonym'):
            initial.pop(key, None)
        source = request.GET.get("source_review", "")
        if source.isdecimal() and len(source) < 19:
            review = get_object_or_404(Review, pk=int(source))
            initial.update(review_text_initial(review))
            initial['source_review_id'] = review.pk
        source_review = getattr(request, '_source_review', None)
        if source_review is not None:
            initial.update(review_text_initial(source_review))
            initial['source_review_id'] = source_review.pk
        # No personal data accepted from query strings.
        return initial

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
        "for_recording",
        "audiobook_blacklisted",
    )
    list_filter = ("audiobook_blacklisted", "for_recording", "anthology", "anthology__is_translated")
    actions = ('block_audiobooks', 'unblock_audiobooks')

    def _set_audiobook_blacklist(self, request, queryset, blocked):
        if not self.has_change_permission(request):
            raise PermissionDenied
        using = queryset.db
        # Lock only Text rows, without nullable anthology joins from the list.
        selected = queryset.values_list('pk', flat=True)
        count = 0
        description = ('Dodano do czarnej listy audiobooków; wyłączono nagrywanie.' if blocked
                       else 'Usunięto z czarnej listy audiobooków; nagrywanie pozostało wyłączone.')
        with transaction.atomic(using=using):
            rows = Text.objects.using(using).filter(pk__in=selected).order_by('pk').select_for_update()
            for obj in rows:
                if obj.audiobook_blacklisted == blocked and not (blocked and obj.for_recording):
                    continue
                obj.audiobook_blacklisted = blocked
                obj.for_recording = False
                obj.save(using=using, update_fields=['audiobook_blacklisted', 'for_recording'])
                self.log_change(request, obj, description)
                count += 1
        self.message_user(request, f'{description} Zmieniono tekstów: {count}.')

    @admin.action(description='Audiobooki: dodaj do czarnej listy', permissions=['change'])
    def block_audiobooks(self, request, queryset):
        self._set_audiobook_blacklist(request, queryset, True)

    @admin.action(description='Audiobooki: usuń z czarnej listy (bez włączania nagrywania)', permissions=['change'])
    def unblock_audiobooks(self, request, queryset):
        self._set_audiobook_blacklist(request, queryset, False)
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
        if obj and obj.anthology_id and obj.anthology.is_novel:
            fields = (*fields, 'authors', 'tags', 'genre', 'anthology')
        return fields if request.user.is_superuser else (*fields, "file_url")

    readonly_fields = ("repeat_workflow_link", "manual_status_link", "coordinator_note_updated_at", "current_workflow_cycle", "import_source", "import_source_row")

    fieldsets = (
        ('Tekst', {'fields': ('title', 'authors', 'anthology', 'chapter_number', 'length', 'tags', 'genre', 'content_warnings', 'file_url',
            'source_author_first_name', 'source_author_last_name', 'source_author_email', 'source_author_pseudonym',
            'source_contract_received', 'source_coauthor_contracts', 'source_update_author_phone')}),
        ('Status i zarządzanie', {'fields': ('manual_status_link', 'repeat_workflow_link')}),
        ('Audiobook', {'fields': ('for_recording', 'audiobook_blacklisted'),
            'description': 'Czarna lista wyłącza nagrywanie. Blokadę można usunąć wyłącznie tutaj lub akcją na liście tekstów. Aby po odblokowaniu przekazać tekst do nagrywania, zaznacz „Do nagrywania”.'}),
        ('Notatki koordynatora', {'classes': ('cms-after-workflow',),
            'fields': ('coordinator_note', 'coordinator_note_updated_at')}),
        ('Dane techniczne i pochodzenie importu', {'classes': ('collapse', 'cms-after-inlines'),
            'fields': ('current_workflow_cycle', 'import_source', 'import_source_row')}),
    )
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get('source_coauthor_contracts')
        source = getattr(request, '_source_review', None)
        if field is not None:
            field.queryset = source.coauthors.filter(has_contract=False) if source is not None else Author.objects.none()
        form.source_review = source
        form.source_error = getattr(request, '_source_form_error', None)
        return form

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None and getattr(request, '_source_review', None) is not None:
            return fieldsets
        confirmations = {'source_contract_received', 'source_coauthor_contracts', 'source_update_author_phone'}
        if obj and obj.anthology_id and obj.anthology.is_novel:
            confirmations.update({'title', 'file_url', 'content_warnings', 'for_recording', 'audiobook_blacklisted'})
            fieldsets = tuple((name, options) for name, options in fieldsets if name != 'Audiobook')
        if obj and obj.anthology_id and obj.anthology.is_translated:
            confirmations.add('authors')
        return tuple((name, {**options, 'fields': tuple(field for field in options['fields'] if field not in confirmations)})
                     for name, options in fieldsets)

    inlines = (WorkflowStageInline, TextNoteInline,)

    def get_inlines(self, request, obj=None):
        if obj and obj.anthology_id and obj.anthology.is_translated:
            return (*self.inlines, TextTranslationInline)
        return self.inlines


    def save_formset(self, request, form, formset, change):
        if isinstance(formset, WorkflowPerformerFormSet):
            formset.save_performers(request.user)
        elif formset.model is TextNote:
            instances = formset.save(commit=False)
            for obj in formset.deleted_objects:
                obj.delete()
            for obj in instances:
                if obj._state.adding:
                    obj.author = request.user
                obj.save()
            formset.save_m2m()
        else:
            super().save_formset(request, form, formset, change)
    list_per_page = 50
    show_full_result_count = False

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if form.instance.anthology_id and form.instance.anthology.is_novel:
            text = form.instance
            profile = text.anthology.novel
            text.authors.set(profile.authors.all())
            text.tags, text.genre = profile.tags, profile.genre
            text.save(update_fields=['tags', 'genre'])
        if not change:
            text = form.instance
            source_review = getattr(request, '_source_review', None)
            if source_review is not None:
                from core.services.reviews import resolve_publication_authors
                author, coauthors = resolve_publication_authors(source_review,
                    contract_received=form.cleaned_data.get('source_contract_received', False),
                    confirmed_coauthor_ids=[a.pk for a in form.cleaned_data.get('source_coauthor_contracts', [])],
                    update_author_phone=form.cleaned_data.get('source_update_author_phone', False))
                text.authors.set([author, *coauthors])
                source_review.author = author
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

        source_review = getattr(request, '_source_review', None)
        if not change and source_review is not None:
            from core.services.reviews import validate_review_publication
            from workflow.anthology_policy import require_working_anthology
            validate_review_publication(source_review, contracts=True)
            require_working_anthology(form.instance)
            authors = list(form.instance.authors.select_for_update())
            if not authors or any(not author.has_contract for author in authors):
                raise ValidationError("Każdy autor tekstu musi mieć potwierdzoną umowę. Nie zapisano tekstu.")
            source_review.copied_text = form.instance
            source_review.save(update_fields=['copied_text', 'author'])

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        try:
            with transaction.atomic():
                return self._validated_changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as error:
            # The save transaction rolled back. Render the original bound form with its error.
            if request.method == 'POST' and getattr(request, '_source_review', None) is not None:
                request._source_form_error = ' '.join(error.messages)
                request._source_review.refresh_from_db()
                with transaction.atomic():
                    return super().changeform_view(request, object_id, form_url, extra_context)
            return HttpResponseBadRequest(' '.join(error.messages), content_type='text/plain; charset=utf-8')

    def _validated_changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        token = request.GET.get('prefill')
        source_id = request.GET.get('source_review', '')
        if object_id is None and (token or source_id):
            if token:
                snapshot = request.session.get('review_text_prefills', {}).get(token, {})
                if snapshot.get('user') != request.user.pk or not 0 <= time.time() - snapshot.get('at', 0) < 900:
                    return HttpResponseBadRequest('Dane formularza wygasły. Zamknij okno i ponownie użyj + w recenzji. Nie zapisano tekstu.')
                source_id = snapshot.get('review_id')
            elif not source_id.isdecimal() or len(source_id) >= 19:
                return HttpResponseBadRequest('Nieprawidłowa recenzja źródłowa.')
            with transaction.atomic():
                review = Review.objects.select_for_update().filter(pk=source_id).first()
                if review is None:
                    return HttpResponseBadRequest('Recenzja nie istnieje. Zamknij okno i odśwież stronę.')
                if review.copied_text_id:
                    return HttpResponseBadRequest('Ta recenzja ma już powiązany tekst. Zamknij okno i odśwież recenzję; nie utworzono duplikatu.')
                from core.services.reviews import validate_review_publication
                try:
                    with transaction.atomic():
                        validate_review_publication(review)
                        request._source_review = review
                        if token and snapshot.get('signature') is not None and snapshot['signature'] != review_source_signature(review):
                            request._source_form_error = 'Recenzja zmieniła się po otwarciu okna. Zamknij okno, sprawdź recenzję i ponownie użyj +.'
                        return super().changeform_view(request, object_id, form_url, extra_context)
                except ValidationError:
                    if getattr(request, '_source_review', None) is not None:
                        raise
                    return HttpResponseBadRequest('Recenzja nie spełnia warunków przeniesienia. Sprawdź jej status i antologię.')
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        repeat_view = self.admin_site.admin_view(self.repeat_form)
        repeat_view.model_admin = self
        return [path('<int:object_id>/powtorz-etapy/', repeat_view, name='texts_text_repeat'), path('<path:object_id>/dodaj-etap/', self.admin_site.admin_view(self.add_stage_view), name='texts_text_add_stage'), path('<path:object_id>/status/', self.admin_site.admin_view(self.change_status_view), name='texts_text_manual_status')] + super().get_urls()

    @admin.display(description='Status tekstu')
    def manual_status_link(self, obj):
        from django.utils.html import format_html
        from workflow.state import current_stage
        if not obj or not obj.pk:
            return 'Zapisz tekst, aby ustawić status.'
        stage = current_stage(list(obj.workflow_stages.filter(workflow_cycle=obj.current_workflow_cycle)))
        label = stage.get_stage_type_display() if stage else 'Brak bieżącego etapu'
        return format_html('{} – <a href="{}">Zmień status / cofnij etap</a> · <a href="{}">Dodaj brakujący etap i wykonawcę</a>', label, reverse('admin:texts_text_manual_status', args=[obj.pk]), reverse('admin:texts_text_add_stage', args=[obj.pk]))

    def add_stage_view(self, request, object_id):
        from django.template.response import TemplateResponse
        from django.shortcuts import redirect
        from workflow.admin_add_stage import add_missing_stage
        from workflow.catalog import active_stage_choices
        from workflow.services import STAGE_ROLES
        from workflow.admin_performer_forms import PerformerChoiceField
        from django.contrib.admin.widgets import AutocompleteSelect
        from django.contrib.auth import get_user_model
        from core.edit_versions import version_of
        if not request.user.is_active or not request.user.is_superuser:
            raise PermissionDenied
        obj = get_object_or_404(self.get_queryset(request), pk=unquote(object_id))
        class AddStageForm(forms.Form):
            kind = forms.ChoiceField(label='Etap', choices=[(k,v) for k,v in active_stage_choices() if k in STAGE_ROLES])
            performer = PerformerChoiceField(label='Wykonawca', required=False,
                queryset=get_user_model().objects.all().order_by('last_name','first_name','pk'),
                widget=AutocompleteSelect(WorkflowRoleAssignment._meta.get_field('assigned_to'), admin.site))
            started_at = forms.DateField(label='Rozpoczęcie', required=False, widget=forms.DateInput(attrs={'type':'date'}, format='%Y-%m-%d'))
            ended_at = forms.DateField(label='Zakończenie', required=False, widget=forms.DateInput(attrs={'type':'date'}, format='%Y-%m-%d'))
            historical = forms.BooleanField(label='Uzupełnij zakończoną pracę historyczną (daty opcjonalne)', required=False, help_text='Nie rozpoczyna zadania ani nie zwiększa obciążenia. Można wybrać dawnego nieaktywnego wykonawcę.')
            version = forms.IntegerField(widget=forms.HiddenInput)
        form = AddStageForm(request.POST if request.method == 'POST' else None, initial={'version':version_of(obj)})
        if request.method == 'POST' and form.is_valid():
            try:
                data = form.cleaned_data.copy()
                stage = add_missing_stage(obj.pk, request.user, **data)
            except (ValidationError, PermissionDenied) as exc:
                form.add_error(None, ' '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                self.log_change(request, obj, 'Zapisano etap i wykonawcę: ' + stage.get_stage_type_display())
                self.message_user(request, 'Zapisano etap i wykonawcę.')
                return redirect('admin:texts_text_change', obj.pk)
        return TemplateResponse(request, 'admin/texts/text/add_stage.html', {**self.admin_site.each_context(request), 'title':'Dodaj brakujący etap: '+obj.title, 'form':form, 'original':obj, 'opts':self.model._meta, 'media':form.media})

    def change_status_view(self, request, object_id):
        from django.template.response import TemplateResponse
        from django.shortcuts import redirect
        from workflow.admin_status import set_admin_status
        from workflow.catalog import active_stage_choices
        from core.edit_versions import version_of
        if not request.user.is_superuser:
            raise PermissionDenied
        obj = get_object_or_404(self.get_queryset(request), pk=unquote(object_id))
        class StatusForm(forms.Form):
            stage = forms.ChoiceField(label='Nowy status / etap', choices=active_stage_choices())
            version = forms.IntegerField(widget=forms.HiddenInput)
            confirm = forms.BooleanField(label='Potwierdzam ręczną korektę statusu i zachowanie wcześniejszych wykonań w historii.')
        form = StatusForm(request.POST if request.method == 'POST' else None, initial={'version':version_of(obj)})
        if request.method == 'POST' and form.is_valid():
            try:
                stage = set_admin_status(obj.pk, form.cleaned_data['stage'], request.user, form.cleaned_data['version'])
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                self.log_change(request, obj, 'Ręczna zmiana statusu: '+stage.get_stage_type_display())
                self.message_user(request, 'Ustawiono status: '+stage.get_stage_type_display()+'. Wcześniejsze wykonania zachowano.')
                return redirect('admin:texts_text_change', obj.pk)
        return TemplateResponse(request, 'admin/texts/text/manual_status.html', {**self.admin_site.each_context(request), 'title':'Zmień status tekstu: '+obj.title, 'form':form, 'original':obj, 'opts':self.model._meta})

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("anthology")
            .prefetch_related("authors")
        )

    @admin.display(description="autorzy")
    def display_authors(self, obj):
        if obj.anthology_id and obj.anthology.is_translated:
            record = getattr(obj, 'translation', None)
            return ', '.join(str(a) for a in record.foreign_authors.all()) if record else '–'
        return ", ".join(
            str(author) for author in obj.authors.all()
        ) or "–"

    @admin.display(description="adresy e-mail")
    def display_author_emails(self, obj):
        if obj.anthology_id and obj.anthology.is_translated:
            record = getattr(obj, 'translation', None)
            return ', '.join(a.email for a in record.foreign_authors.all() if a.email) if record else '–'
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

    def delete_queryset(self, request, queryset):
        # Bulk deletion does not carry an object_id for the middleware.
        with transaction.atomic():
            parent_ids = queryset.values_list('text_id', flat=True)
            list(Text.objects.select_for_update().filter(pk__in=parent_ids).order_by('pk'))
            super().delete_queryset(request, queryset)

    def save_model(self, request, obj, form, change):
        if not self.has_superuser_access(request):
            raise PermissionDenied

        if obj._state.adding:
            obj.author = request.user

        super().save_model(request, obj, form, change)

    @admin.display(description="treść")
    def note_preview(self, obj):
        return content_preview(obj.content)




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
    verbose_name = "Ogólne uwagi do zgłoszenia"
    verbose_name_plural = "Ogólne uwagi do zgłoszenia"
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
    verbose_name_plural = "Oceny recenzentów"
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
        "author_pseudonym__plcontains",
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
                    "file_url",
                    "is_hidden",
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
            "author_pseudonym",
                    "email",
                    "phone_number",
                    "author_message",
                ),
            },
        ),
        (
            "Decyzja",
            {
                "fields": (
                    "status",
                    "author_notified_at",
                ),
            },
        ),
        (
            "Proces wydawniczy",
            {"classes": ("cms-after-inlines",), "fields": ("copied_text", "confirm_existing_text", "confirm_source_mismatch", "publication_detached")},
        ),
        (
            "Kontrola zgłoszenia",
            {
                "classes": ("collapse", "cms-after-inlines"),
                "fields": (
                    "confirm_submission_warnings",
                    "submission_warnings_token",
                ),
            },
        ),
        ('Historia zgłoszenia', {'classes': ('collapse', 'cms-after-inlines'),
            'fields': ('created_at', 'decision_at', 'old_reviews')}),
    )

    ordering = ("-created_at", "-pk")
    date_hierarchy = "created_at"
    inlines = (ReviewAssignmentInline, ReviewersInline)
    list_per_page = 50
    show_full_result_count = False

    class Media:
        js = ("texts/admin/review_author_autofill.js", "texts/admin/review_text_link.js",)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.old_reviews:
            return ("created_at", "old_reviews")
        return (*super().get_readonly_fields(request, obj), "status", "author_notified_at", *(("old_reviews",) if obj else ()))

    def get_urls(self):
        return [
            path('prepare-text/', self.admin_site.admin_view(self.prepare_text_view), name='texts_review_prepare_text'),
            path(
                "author-details/<int:author_id>/",
                self.admin_site.admin_view(self.author_details_view),
                name="texts_review_author_details",
            ),
        ] + super().get_urls()

    def prepare_text_view(self, request):
        if not self.has_superuser_access(request):
            raise PermissionDenied
        if request.method != 'POST':
            return JsonResponse({'error': 'POST required'}, status=405)
        try:
            review = Review.objects.get(pk=int(request.POST.get('review_id', '')))
        except (ValueError, Review.DoesNotExist):
            return JsonResponse({'error': 'Najpierw zapisz recenzję, a następnie użyj +.'}, status=400)
        if review.copied_text_id:
            return JsonResponse({'error': 'Recenzja ma już powiązany tekst. Odśwież formularz.'}, status=409)
        from core.services.reviews import validate_review_publication
        try:
            validate_review_publication(review)
        except ValidationError as error:
            return JsonResponse({'error': ' '.join(error.messages)}, status=400)
        data = review_text_initial(review)
        # Refuse a mixture of unsaved form data and the persisted source.
        for key in ('title', 'genre', 'length', 'content_warnings', 'file_url', 'anthology', 'source_author_first_name',
                    'source_author_last_name', 'source_author_email', 'source_author_pseudonym'):
            if key in request.POST and request.POST[key] != str(data[key] if data[key] is not None else ''):
                return JsonResponse({'error': 'Najpierw zapisz zmiany w recenzji, a następnie ponownie użyj +.'}, status=409)
        if ('source_author_id' in request.POST and request.POST['source_author_id'] != str(review.author_id or '')) or (
                'source_coauthors_present' in request.POST and set(request.POST.getlist('source_coauthors')) !=
                {str(pk) for pk in review.coauthors.values_list('pk', flat=True)}):
            return JsonResponse({'error': 'Najpierw zapisz zmiany autorów w recenzji, a następnie ponownie użyj +.'}, status=409)
        now = time.time()
        snapshots = {key: value for key, value in request.session.get('review_text_prefills', {}).items()
                     if now - value.get('at', 0) < 900}
        snapshots = dict(list(snapshots.items())[-9:])
        token = secrets.token_urlsafe(24)
        snapshots[token] = {'user': request.user.pk, 'at': now, 'data': data, 'review_id': review.pk, 'signature': review_source_signature(review)}
        request.session['review_text_prefills'] = snapshots
        url = reverse(f'{self.admin_site.name}:texts_text_add')
        return JsonResponse({'url': url + '?_popup=1&prefill=' + token})

    def render_change_form(self, request, context, *args, **kwargs):
        response = super().render_change_form(request, context, *args, **kwargs)
        field = context['adminform'].form.fields.get('copied_text')
        if field:
            widget = getattr(field.widget, 'widget', field.widget)
            widget.attrs['data-source-review-id'] = str(context.get('original').pk) if context.get('original') else ''
            widget.attrs['data-prepare-text-url'] = reverse(f'{self.admin_site.name}:texts_review_prepare_text')
        return response

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
                "pseudonym": author.pseudonym,
                "phone_number": author.phone_number,
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

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), "title": "Zgłoszenia do recenzji"})

    def changeform_view(
        self,
        request,
        object_id=None,
        form_url="",
        extra_context=None,
    ):
        extra_context = {**(extra_context or {}), "title": "Edycja zgłoszenia do recenzji" if object_id else "Dodaj zgłoszenie do recenzji"}
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

        identity_changed = bool({'author', 'email', 'coauthors'} & set(form.changed_data))
        if not change or (identity_changed and not obj.old_reviews and not obj.copied_text_id and obj.status in (Review.Status.NEW, Review.Status.IN_REVIEW, Review.Status.TO_DECIDE)):
            from texts.blacklist import apply_blacklist
            apply_blacklist(obj, coauthors=form.cleaned_data.get("coauthors", ()))
            if obj.is_hidden and obj.pk:
                obj._clear_unfinished_blacklisted_assignments = True
        super().save_model(request, obj, form, change)

        # Linking a review must not overwrite the editorial text.

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if getattr(form.instance, '_clear_unfinished_blacklisted_assignments', False):
            form.instance.assignments.filter(opinion__in=('', Reviewers.Opinion.READING)).delete()

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

from . import catalog_admin  # noqa: E402,F401


@admin.register(ExtractVolume)
class ExtractVolumeAdmin(admin.ModelAdmin):
    list_display = ('number', 'anthology')
    readonly_fields = ('number', 'anthology')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExtractTextLink)
class ExtractTextLinkAdmin(admin.ModelAdmin):
    list_display = ('source_title', 'extract', 'text')
    readonly_fields = ('source_title', 'title_key', 'extract', 'text')
    search_fields = ('source_title',)
    list_select_related = ('extract', 'text')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
