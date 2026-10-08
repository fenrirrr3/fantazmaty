from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

User = get_user_model()


class IdentityFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"] = forms.CharField(label="Nazwa użytkownika", max_length=150)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
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

from core.models import AnthologyCorrection


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
    list_display = ("anthology", "story_title", "status", "submitted_by", "created_at")
    list_filter = ("status", "anthology")
    search_fields = ("story_title__plcontains", "problem__plcontains", "fragment__plcontains")
    readonly_fields = ("created_at", "updated_at")

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    has_add_permission = has_view_permission



from core.permissions import is_coordinator
from core.models import Recruitment
from texts.models import Extract
from core.intake_forms import RecruitmentForm, ExtractForm


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


@admin.register(Recruitment)
class RecruitmentAdmin(CoordinatorIntakeAdmin):
    form = RecruitmentForm
    list_display = ('first_name', 'last_name', 'email', 'mail_subject', 'department', 'submitted_at', 'accepted', 'notified', 'notified_at')
    list_filter = ('department', 'status', 'notified', 'submitted_at')
    search_fields = ('first_name__plcontains', 'last_name__plcontains', 'email__plcontains', 'notes__plcontains', 'mail_subject__plcontains', 'mail_sender__plcontains')
    readonly_fields = ('notified_at', 'updated_at', 'mail_sender', 'mail_subject', 'mail_received_at', 'mail_roles', 'mail_body')
    fields = (*RecruitmentForm.Meta.fields, 'mail_sender', 'mail_subject', 'mail_received_at', 'mail_roles', 'mail_body', 'notified_at', 'updated_at')

    @admin.display(boolean=True, description='Przyjęty/Odrzucony', ordering='status')
    def accepted(self, obj):
        return obj.accepted


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


from people.models import Vacation
from texts.models import AnthologyTask, Reviewers, ReviewAssignment
from authors.admin import SuperuserOnlyAdminMixin


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

    def delete_model(self, request, obj):
        from core.services.vacations import cancel_planned_vacation
        cancel_planned_vacation(user=request.user, vacation_id=obj.pk)


@admin.register(AnthologyTask)
class AnthologyTaskAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('anthology', 'task_type', 'status', 'assigned_to', 'commissioned_at')
    search_fields = ('anthology__title__plcontains', 'assigned_to__first_name__plcontains',
                     'assigned_to__last_name__plcontains', 'assigned_to__email__plcontains')
    list_filter = ('task_type', 'status', 'anthology', 'anthology__status')
    autocomplete_fields = ('anthology', 'assigned_to')
    readonly_fields = ('anthology', 'task_type', 'commissioned_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

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


from core.models import UserActivity


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


from core.models import WorkflowEvent

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





from core.models import MailboxConnection


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

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get('password'):
            instance.set_password(self.cleaned_data['password'])
        if commit:
            instance.save()
        return instance


from core.admin_newsletter_recovery import NewsletterRecoveryAdminMixin


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
