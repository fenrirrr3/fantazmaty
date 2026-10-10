from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from core.models import AnthologyCorrection
from core.permissions import is_coordinator
from core.models import Recruitment
from texts.models import Extract
from core.intake_forms import ExtractForm
from people.models import Vacation
from texts.models import AnthologyTask, Reviewers, ReviewAssignment
from authors.admin import SuperuserOnlyAdminMixin
from core.models import UserActivity
from core.models import WorkflowEvent
from core.models import MailboxConnection
from core.admin_newsletter_recovery import NewsletterRecoveryAdminMixin
from core.models import PublicAudiobookSettings
from core.models import Audiobook, AudiobookStage
from core.models import AudioContributor
from core.models import PostLayoutAssignment
from core.audiobook_forms import AudiobookProductionForm


class AudiobookAdminForm(AudiobookProductionForm):
    class Meta(AudiobookProductionForm.Meta):
        exclude = ()

    def clean_additional_links(self):
        return self.cleaned_data.get('additional_links') or []


@admin.register(PostLayoutAssignment)
class PostLayoutAssignmentAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    from core.post_layout import AssignmentEditForm
    form = AssignmentEditForm
    list_display = ('anthology', 'proofreader', 'page_from', 'page_to', 'status', 'assigned_start', 'work_start', 'completed_on', 'deleted_at')
    list_filter = ('status', 'historical', ('deleted_at', admin.EmptyFieldListFilter), 'anthology')
    list_select_related = ('anthology', 'proofreader')
    search_fields = ('anthology__title', 'proofreader__first_name', 'proofreader__last_name')
    readonly_fields = ('anthology', 'status', 'assigned_end', 'work_end', 'created_at', 'created_by', 'historical',
        'deleted_at', 'deleted_by', 'management_link')
    fields = ('anthology', 'proofreader', 'page_from', 'page_to', 'status', 'assigned_start', 'assigned_end',
        'work_start', 'work_end', 'completed_on', 'created_at', 'created_by', 'historical', 'deleted_at', 'deleted_by', 'management_link')
    actions = None

    @admin.display(description='Zarządzanie przydziałami i statusami')
    def management_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Otwórz Korektę poskładową</a>', reverse('core:post_layout'))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return self.has_superuser_access(request) and not (obj and obj.deleted_at)

    def has_change_permission(self, request, obj=None):
        # Usunięte wpisy są historią – przywraca się je na stronie Korekty poskładowej.
        return self.has_superuser_access(request) and not (obj and obj.deleted_at)

    def delete_model(self, request, obj):
        # Jak na stronie: usunięcie ukrywa wpis, historia pracy zostaje.
        from django.utils import timezone
        obj.deleted_at, obj.deleted_by = timezone.now(), request.user
        obj.version += 1
        obj.save(update_fields=['deleted_at', 'deleted_by', 'version'])

    def save_model(self, request, obj, form, change):
        obj.version += 1
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(AudioContributor)
class AudioContributorAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'email', 'user')
    search_fields = ('name', 'email')
    autocomplete_fields = ('user',)
    fields = ('name', 'email', 'user', 'works_link')
    readonly_fields = ('works_link',)
    actions = ('merge_contacts',)

    @admin.action(description='Połącz zaznaczone profile w jeden (zostaje najstarszy)')
    def merge_contacts(self, request, queryset):
        from django.contrib import messages
        from django.db import transaction
        from core.models import Audiobook
        with transaction.atomic():
            people = list(queryset.select_for_update().order_by('pk'))
            if len(people) < 2:
                self.message_user(request, 'Zaznacz co najmniej dwa profile.', messages.ERROR)
                return
            keep, *duplicates = people
            ids = [person.pk for person in duplicates]
            for role in ('narrator', 'engineer'):
                for audio in Audiobook.objects.select_for_update().filter(**{f'{role}_contact_id__in': ids}):
                    setattr(audio, f'{role}_contact', keep)
                    audio.save(update_fields=[f'{role}_contact'])
            if not keep.email:
                keep.email = next((person.email for person in duplicates if person.email), '')
            if not keep.user_id:
                keep.user_id = next((person.user_id for person in duplicates if person.user_id), None)
            AudioContributor.objects.filter(pk__in=ids).delete()
            keep.save()
        self.message_user(request, f'Połączono {len(ids) + 1} profile w „{keep.name}”.', messages.SUCCESS)

    @admin.display(description='Nagrania')
    def works_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Zrealizowane nagrania</a>', reverse('core:audio_contributor', args=[obj.pk])) if obj.pk else 'Zapisz profil.'


@admin.register(AudiobookStage)
class AudiobookStageAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('text', 'stage_type', 'performer', 'started_at', 'ended_at', 'is_completed')
    list_filter = ('stage_type', 'is_completed')
    search_fields = ('text__title',)
    list_select_related = ('text', 'performer')
    readonly_fields = ('text', 'stage_type', 'performer', 'started_at', 'ended_at', 'is_completed')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Audiobook)
class AudiobookAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = AudiobookAdminForm
    list_display = ('text', 'status', 'narrator_name', 'engineer_name', 'proofreader', 'premiere_date')
    list_filter = ('status', 'text__anthology')
    search_fields = ('text__title', 'narrator_name', 'engineer_name', 'proofreader__last_name')
    list_select_related = ('text__anthology', 'proofreader')
    autocomplete_fields = ('text',)
    fieldsets = (
        ('Tekst i status', {'fields': ('text', 'status', 'production_link')}),
        ('Lektor', {'fields': ('narrator_name', 'narrator_email', 'recording_started_at', 'corrections_started_at')}),
        ('Korektor audiobooka', {'fields': ('proofreader', 'proofreading_started_at')}),
        ('Montaż', {'fields': ('engineer_name', 'engineer_email', 'editing_started_at')}),
        ('Publikacja', {'fields': ('awaiting_publication_started_at', 'premiere_date', 'youtube_url', 'hearthis_url', 'additional_links')}),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = ('status', 'production_link', 'recording_started_at', 'proofreading_started_at', 'corrections_started_at',
            'editing_started_at', 'awaiting_publication_started_at')
        if obj and not self.proofreader_editable(obj):
            # Jak na stronie: korektora zmienia się tylko w trakcie etapu Korekta, przed jego zakończeniem.
            fields = (*fields, 'proofreader')
        return (*fields, 'text') if obj else fields

    @staticmethod
    def proofreader_editable(obj):
        from core.selectors.audio_proofreading import can_assign_proofreader, corrections
        return obj.status == Audiobook.Status.PROOFREADING and can_assign_proofreader(
            obj, corrections().filter(text_id=obj.text_id))

    @admin.display(description='Etapy produkcji')
    def production_link(self, obj):
        if not obj or not obj.text_id:
            return 'Zapisz audiobook, aby otworzyć etapy produkcji.'
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Otwórz podgląd audiobooka i zarządzaj etapami</a>',
            reverse('core:audiobook_detail', args=[obj.text_id]))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'text':
            from texts.models import Text
            from texts.production import active_production_texts
            kwargs['queryset'] = active_production_texts(Text.objects.filter(
                for_recording=True, audiobook_blacklisted=False, audiobook__isnull=True))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_delete_permission(self, request, obj=None):
        # Disabling recording preserves its production history.
        return False


@admin.register(PublicAudiobookSettings)
class PublicAudiobookSettingsAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    fields = ('mega_url', 'guidelines')

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not PublicAudiobookSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

User = get_user_model()


class IdentityFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"] = forms.CharField(label="Nazwa użytkownika", max_length=150)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        # Imię i nazwisko trafiają też do profilu osoby, który mieści 100 znaków.
        from people.models import Person
        for name in ("first_name", "last_name"):
            limit = Person._meta.get_field(name).max_length
            self.fields[name].max_length = limit
            self.fields[name].widget.attrs["maxlength"] = str(limit)
            from django.core.validators import MaxLengthValidator
            self.fields[name].validators.append(MaxLengthValidator(limit))
        self.fields["email"].required = True
        self.fields["email"].label = "Adres e-mail (login)"
        self.fields["username"].help_text = "Wewnętrzna nazwa konta. Do logowania służy adres e-mail."

    def _get_validation_exclusions(self):
        return super()._get_validation_exclusions() | {"username"}

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if any(ord(char) < 32 for char in value):
            raise forms.ValidationError("Nazwa nie może zawierać znaków sterujących.")
        if User.objects.filter(username__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Ta nazwa użytkownika jest już zajęta.")
        return value

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_email(self):
        from core.account_identity import validate_account_email
        return validate_account_email(
            self.cleaned_data["email"], exclude_pk=self.instance.pk,
            using=self.instance._state.db or "default",
        )

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())


class AccountCreationForm(IdentityFormMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")


class AccountChangeForm(IdentityFormMixin, UserChangeForm):
    pass


class AccountAdmin(UserAdmin):
    list_display = ("username", "first_name", "last_name", "email", "is_active", "is_staff", "is_superuser")
    readonly_fields = (*UserAdmin.readonly_fields, "profile_link")
    fieldsets = (*UserAdmin.fieldsets, ("Profil członka zespołu", {"fields": ("profile_link",)}))

    @admin.display(description="Profil osoby")
    def profile_link(self, obj):
        from people.models import Person
        from django.urls import reverse
        from django.utils.html import format_html
        person = Person.objects.filter(user_id=obj.pk).first() if obj and obj.pk else None
        if person is None:
            return "Brak powiązanego profilu"
        return format_html('<a href="{}">Otwórz profil: {}</a>', reverse('admin:people_person_change', args=[person.pk]), person)

    def save_model(self, request, obj, form, change):
        obj._cms_edit_profile_name = True
        super().save_model(request, obj, form, change)
        from people.models import Person
        person = Person.objects.filter(user_id=obj.pk).first()
        if person and {'first_name', 'last_name'}.intersection(form.changed_data):
            person.first_name, person.last_name = obj.first_name, obj.last_name
            person.save(update_fields=['first_name', 'last_name'])

    search_fields = ('first_name__plcontains', 'last_name__plcontains', 'email__plcontains', 'username__plcontains')
    ordering = ('last_name', 'first_name', 'pk')
    form = AccountChangeForm
    add_form = AccountCreationForm
    add_fieldsets = ((None, {"classes": ("wide",), "fields": (
        "username", "first_name", "last_name", "email", "password1", "password2",
    )}),)


if admin.site.is_registered(User):
    admin.site.unregister(User)
admin.site.register(User, AccountAdmin)



@admin.register(AnthologyCorrection)
class AnthologyCorrectionAdmin(admin.ModelAdmin):
    def has_change_permission(self, request, obj=None):
        return bool(not (obj and obj.is_resolved) and super().has_change_permission(request, obj))

    def has_delete_permission(self, request, obj=None):
        return bool(obj is not None and not obj.is_resolved and super().has_delete_permission(request, obj))

    from core.correction_forms import AdminCorrectionForm
    form = AdminCorrectionForm
    class Media:
        js = ("core/corrections.js",)
    list_display = ("anthology", "story_title", "status", "submitted_by", "reporter_name", "created_at")
    list_filter = ("status", "anthology")
    search_fields = ("story_title__plcontains", "problem__plcontains", "fragment__plcontains")
    readonly_fields = ("created_at", "updated_at")

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    has_add_permission = has_view_permission





class CoordinatorIntakeAdmin(admin.ModelAdmin):
    """Uprawnienia modelu nie omijają wymaganej roli koordynatora."""
    def has_module_permission(self, request):
        return is_coordinator(request.user)

    def has_view_permission(self, request, obj=None):
        return is_coordinator(request.user)

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission
    readonly_fields = ('created_at', 'updated_at')


class RecruitmentRoleFilter(admin.SimpleListFilter):
    title = 'rola zgłoszenia'
    parameter_name = 'recruitment_role'

    def lookups(self, request, model_admin):
        from core.selectors.recruitment import role_choices
        return role_choices()

    def queryset(self, request, queryset):
        from core.selectors.recruitment import filter_role, role_choices
        if self.value() in dict(role_choices()):
            return filter_role(queryset, self.value())
        return queryset


@admin.register(Recruitment)
class RecruitmentAdmin(CoordinatorIntakeAdmin):
    from core.recruitment_admin_forms import RecruitmentAdminForm
    form = RecruitmentAdminForm
    list_display = ('candidate_display', 'email', 'mail_subject', 'roles_display', 'submitted_at', 'accepted', 'notified', 'notified_at')
    list_filter = (RecruitmentRoleFilter, 'status', 'notified', 'submitted_at')
    search_fields = ('first_name__plcontains', 'last_name__plcontains', 'email__plcontains', 'notes__plcontains', 'applicant_name__plcontains', 'decision_reason__plcontains', 'role_decisions__decision_reason__plcontains', 'role_decisions__unofficial_notes__plcontains', 'mail_subject__plcontains', 'mail_sender__plcontains')
    readonly_fields = ('notified_at', 'updated_at', 'mail_sender', 'mail_subject', 'mail_received_at', 'mail_body', 'decisions_link', 'archived_decisions', 'legacy_decision_reason', 'legacy_unofficial_notes')
    fields = (*RecruitmentAdminForm.Meta.fields, 'mail_sender', 'mail_subject', 'mail_received_at', 'mail_body', 'decisions_link', 'archived_decisions', 'legacy_decision_reason', 'legacy_unofficial_notes', 'notified_at', 'updated_at')

    def get_readonly_fields(self, request, obj=None):
        from core.permissions import can_use_recruitment_mailbox
        return self.readonly_fields if can_use_recruitment_mailbox(request.user) else (*self.readonly_fields, 'notified')

    @admin.display(description='Role zgłoszenia')
    def roles_display(self, obj):
        from core.selectors.recruitment import record_roles, role_choices
        roles = record_roles(obj.mail_roles, obj.department)
        return ', '.join(label for role, label in role_choices() if role in roles)

    @admin.display(description='Archiwalne decyzje usuniętych ról')
    def archived_decisions(self, obj):
        from core.selectors.recruitment import record_roles
        from django.utils.html import format_html_join
        if not obj or not obj.pk:
            return 'Brak'
        rows = list(obj.role_decisions.exclude(role__in=record_roles(obj.mail_roles, obj.department)))
        return format_html_join('', '<p><strong>{} – {}</strong><br>Uzasadnienie: {}<br>Notatki: {}</p>',
                                ((row.role_label, row.get_status_display(), row.decision_reason or '–', row.unofficial_notes or '–') for row in rows)) if rows else 'Brak'

    @admin.display(description='Dawne wspólne uzasadnienie (archiwalne)')
    def legacy_decision_reason(self, obj):
        return obj.decision_reason

    @admin.display(description='Dawne wspólne notatki (archiwalne)')
    def legacy_unofficial_notes(self, obj):
        return obj.unofficial_notes

    @admin.display(description='Decyzje dla poszczególnych ról')
    def decisions_link(self, obj):
        if not obj or not obj.pk:
            return 'Zapisz zgłoszenie, aby ustawić decyzje dla ról.'
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Edytuj osobne decyzje, uzasadnienia i notatki</a>', reverse('core:recruitment_detail', args=[obj.pk]))

    @admin.display(description='Imię i nazwisko', ordering='applicant_name')
    def candidate_display(self, obj):
        return obj.full_name or obj.mail_sender or '–'

    @admin.display(description='Przyjęty/Odrzucony', ordering='status')
    def accepted(self, obj):
        return obj.decision_display


@admin.register(Extract)
class ExtractAdmin(CoordinatorIntakeAdmin):
    form = ExtractForm
    readonly_fields = ('updated_at', 'submitted_at', 'status')
    list_display = ('full_name', 'email', 'phone_number', 'submitted_titles_display', 'dates_display', 'recruitment', 'accepted_display', 'rejected_display')
    list_filter = ('status', 'recruitment', 'submitted_at')
    search_fields = ('full_name__plcontains', 'email__plcontains', 'phone_number__plcontains', 'title__plcontains', 'accepted_titles__plcontains', 'rejected_titles__plcontains', 'recruitment__plcontains')
    list_select_related = ('author',)
    fields = (*ExtractForm.Meta.fields, 'submitted_at', 'status', 'updated_at')


    @staticmethod
    def _lines(value):
        from django.utils.html import format_html_join
        return format_html_join('', '{}<br>', ((line,) for line in value.splitlines())) if value else '–'

    @admin.display(description='Nadesłane tytuły')
    def submitted_titles_display(self, obj):
        return self._lines(obj.title)

    @admin.display(description='Daty nadesłania', ordering='submitted_at')
    def dates_display(self, obj):
        return self._lines(obj.submission_dates)

    @admin.display(description='Przyjęte')
    def accepted_display(self, obj):
        return self._lines(obj.accepted_titles)

    @admin.display(description='Odrzucone')
    def rejected_display(self, obj):
        return self._lines(obj.rejected_titles)




@admin.register(Vacation)
class VacationAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('person', 'start_date', 'end_date', 'until_revoked')
    list_filter = ('until_revoked', 'start_date')
    search_fields = ('person__first_name__plcontains', 'person__last_name__plcontains')
    autocomplete_fields = ('person',)
    readonly_fields = ('created_at',)

    def get_readonly_fields(self, request, obj=None):
        return (*self.readonly_fields, *(("person",) if obj else ()))

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        # Serialize validation with other vacation writes for the same person.
        from django.db import transaction
        from people.models import Person
        with transaction.atomic():
            if request.method == "POST" and not object_id:
                person_id = request.POST.get("person", "")
                if person_id.isdecimal() and len(person_id) < 19:
                    Person.objects.select_for_update().filter(pk=int(person_id)).first()
            return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        from django.db import transaction
        from django.utils import timezone
        from people.models import Person
        from core.services.vacations import _sync_person_leave
        with transaction.atomic():
            person = Person.objects.select_for_update().get(pk=obj.person_id)
            super().save_model(request, obj, form, change)
            _sync_person_leave(person, now=timezone.now())

    def has_delete_permission(self, request, obj=None):
        from django.utils import timezone
        return bool(obj and obj.start_date > timezone.localdate() and super().has_delete_permission(request, obj))

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)

        class VacationAdminForm(form):
            def clean(self):
                data = super().clean()
                person = data.get('person')
                if self.instance._state.adding and person is not None and not person.is_active:
                    # Jak na stronie: nie planujemy urlopów osobom spoza zespołu.
                    self.add_error('person', 'Ta osoba nie jest już w zespole – nie można dodać jej urlopu.')
                return data

        return VacationAdminForm

    def delete_model(self, request, obj):
        # Superuser odwołuje zaplanowany urlop także osobie, która odeszła z zespołu
        # (usługa na stronie by tego odmówiła i kończyło się błędem 500).
        from django.db import transaction
        from django.utils import timezone
        from people.models import Person
        from django.core.exceptions import PermissionDenied
        from core.services.vacations import _sync_person_leave
        with transaction.atomic():
            person = Person.objects.select_for_update().get(pk=obj.person_id)
            now = timezone.now()
            if obj.start_date <= timezone.localdate(now):
                raise PermissionDenied('Usunąć można tylko urlop, który jeszcze się nie rozpoczął.')
            obj.delete()
            _sync_person_leave(person, now=now)


class AnthologyTaskAdminForm(forms.ModelForm):
    """Gotowa audiodeskrypcja musi mieć treść – inaczej sygnał oznaczyłby pustą jako zakończoną."""

    class Meta:
        model = AnthologyTask
        fields = '__all__'

    def clean(self):
        data = super().clean()
        task_type = data.get('task_type') or self.instance.task_type
        status = data.get('status')
        was_ready = bool(self.instance.pk and AnthologyTask.objects.filter(
            pk=self.instance.pk, status=AnthologyTask.Status.READY).exists())
        if task_type == 'audio_description' and status == AnthologyTask.Status.READY and not was_ready:
            from core.models import AudioDescription
            anthology_id = self.instance.anthology_id or getattr(data.get('anthology'), 'pk', None)
            description = AudioDescription.objects.filter(anthology_id=anthology_id).first()
            if description is None or not (description.content or '').strip():
                self.add_error('status', 'Audiodeskrypcja nie ma treści. Uzupełnij ją na stronie Audiodeskrypcji '
                                         'albo wybierz „Nie dotyczy”.')
        return data


@admin.register(AnthologyTask)
class AnthologyTaskAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = AnthologyTaskAdminForm
    list_display = ('anthology', 'task_type', 'status', 'assigned_to', 'commissioned_at')
    search_fields = ('anthology__title__plcontains', 'assigned_to__first_name__plcontains',
                     'assigned_to__last_name__plcontains', 'assigned_to__email__plcontains')
    list_filter = ('task_type', 'status', 'anthology', 'anthology__status')
    autocomplete_fields = ('anthology', 'assigned_to')
    readonly_fields = ('anthology', 'task_type', 'commissioned_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(self.has_superuser_access(request) and obj is not None
            and obj.anthology_id in getattr(request, '_deleting_anthologies', ())
            and obj.status == AnthologyTask.Status.NOT_COMMISSIONED and obj.assigned_to_id is None)

    list_select_related = ('anthology', 'assigned_to')


class ServiceOwnedReviewAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    """Zmiana przydziałów wymaga blokad i walidacji w widoku zgłoszenia."""
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields if field.name not in ("notes", "general_notes"))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return bool(self.has_superuser_access(request) and obj is not None)

    def has_delete_permission(self, request, obj=None):
        return bool(self.has_superuser_access(request) and obj is not None and obj.review.old_reviews)


@admin.register(ReviewAssignment)
class ReviewAssignmentAdmin(ServiceOwnedReviewAdmin):
    list_display = ('review', 'user', 'position', 'opinion', 'assigned_at')
    list_filter = ('opinion',)
    search_fields = ('review__title__plcontains', 'user__first_name__plcontains', 'user__last_name__plcontains')
    list_select_related = ('review', 'user')


@admin.register(Reviewers)
class ReviewersAdmin(ServiceOwnedReviewAdmin):
    list_display = ('__str__',)




@admin.register(UserActivity)
class UserActivityAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action', 'target', 'method', 'status_code')
    list_filter = ('method', 'created_at')
    search_fields = ('actor', 'action', 'target')
    readonly_fields = tuple(field.name for field in UserActivity._meta.fields)
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False



@admin.register(WorkflowEvent)
class WorkflowEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'title', 'previous_status', 'next_status', 'actor_name', 'channel', 'status')
    list_filter = ('status', 'channel')
    search_fields = ('title', 'actor_name')
    readonly_fields = tuple(f.name for f in WorkflowEvent._meta.fields)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False







class MailboxConnectionForm(forms.ModelForm):
    password = forms.CharField(label='Hasło / hasło aplikacji', required=False,
        strip=False, widget=forms.PasswordInput(render_value=False),
        help_text='Pozostaw puste, aby zachować zapisane hasło. Hasło nie jest wyświetlane po zapisaniu.')

    class Meta:
        model = MailboxConnection
        fields = ('name', 'purpose', 'host', 'port', 'security', 'username', 'password', 'folder', 'recruitment_subjects', 'is_active')

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        if not password and not self.instance.encrypted_password:
            raise forms.ValidationError('Podaj hasło skrzynki lub hasło aplikacji.')
        return password

    def clean(self):
        data = super().clean()
        # Import rekrutacji wymaga dokładnie jednej aktywnej skrzynki tego typu.
        if data.get('is_active') and data.get('purpose') == MailboxConnection.Purpose.RECRUITMENT:
            others = MailboxConnection.objects.filter(purpose=MailboxConnection.Purpose.RECRUITMENT, is_active=True)
            if self.instance.pk:
                others = others.exclude(pk=self.instance.pk)
            if others.exists():
                raise forms.ValidationError('Aktywna może być tylko jedna skrzynka rekrutacji. Najpierw wyłącz poprzednią.')
        return data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get('password'):
            instance.set_password(self.cleaned_data['password'])
        if commit:
            instance.save()
        return instance




@admin.register(MailboxConnection)
class MailboxConnectionAdmin(NewsletterRecoveryAdminMixin, SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = MailboxConnectionForm
    list_display = ('name', 'purpose', 'host', 'username', 'folder', 'is_active')
    list_filter = ('purpose', 'is_active')
    fields = ('name', 'purpose', 'host', 'port', 'security', 'username', 'password', 'folder', 'recruitment_subjects', 'is_active')
    search_fields = ('name', 'host', 'username')

    def get_changeform_initial_data(self, request):
        from urllib.parse import parse_qs
        initial = super().get_changeform_initial_data(request)
        filters = parse_qs(request.GET.get('_changelist_filters', ''))
        if filters.get('purpose__exact') == [MailboxConnection.Purpose.RECRUITMENT]:
            initial['purpose'] = MailboxConnection.Purpose.RECRUITMENT
        return initial

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        purpose = request.POST.get('purpose') if request.method == 'POST' else (
            obj.purpose if obj else self.get_changeform_initial_data(request).get('purpose'))
        if purpose == MailboxConnection.Purpose.RECRUITMENT:
            fields.remove('recruitment_subjects')
        return fields


from core.audio_description_admin import AudioDescriptionAdmin  # noqa: E402,F401
