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

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())


class AccountCreationForm(IdentityFormMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")


class AccountChangeForm(IdentityFormMixin, UserChangeForm):
    pass


class AccountAdmin(UserAdmin):
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
    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission


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
    list_display = ('first_name', 'last_name', 'email', 'department', 'submitted_at', 'status', 'notified', 'notified_at')
    list_filter = ('department', 'status', 'notified', 'submitted_at')
    search_fields = ('first_name__plcontains', 'last_name__plcontains', 'email__plcontains', 'notes__plcontains')
    readonly_fields = ('notified_at', 'updated_at')
    fields = (*RecruitmentForm.Meta.fields, 'notified_at', 'updated_at')


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


@admin.register(AnthologyTask)
class AnthologyTaskAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('anthology', '__str__')
    search_fields = ('anthology__title__plcontains',)
    list_filter = ('anthology',)
    autocomplete_fields = ('anthology',)


class ServiceOwnedReviewAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    """Zmiana przydziałów wymaga blokad i walidacji w widoku zgłoszenia."""
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReviewAssignment)
class ReviewAssignmentAdmin(ServiceOwnedReviewAdmin):
    list_display = ('review', 'user', 'position', 'opinion', 'assigned_at')
    list_filter = ('opinion',)
    search_fields = ('review__title__plcontains', 'user__first_name__plcontains', 'user__last_name__plcontains')
    list_select_related = ('review', 'user')


@admin.register(Reviewers)
class ReviewersAdmin(ServiceOwnedReviewAdmin):
    list_display = ('__str__',)
