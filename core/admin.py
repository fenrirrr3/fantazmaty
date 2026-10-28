from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm, UserCreationForm
from core.filtering import positive_id
from core.models import AnthologyCorrection
from core.models import Recruitment
from texts.models import Extract
from core.intake_forms import ExtractForm
from people.models import Vacation
from texts.models import AnthologyTask, Reviewers, ReviewAssignment
from authors.admin import SuperuserOnlyAdminMixin
from core.admin_access import ReadOnlyAdminMixin, SingleRecordAdminMixin
from core.models import UserActivity
from core.models import WorkflowEvent
from core.models import MailboxConnection
from core.models import PublicAudiobookSettings
from core.models import Audiobook, AudiobookStage
from core.models import AudioContributor
from core.models import PostLayoutAssignment
from core.models import SocialCalendarEntry, SocialMediaEntry, SocialMediaQuote, TextSocialMedia
from core.account_identity import person_name
from core.audiobook_forms import AudiobookProductionForm
from core import notification_admin  # noqa: F401 – rejestracja panelu powiadomień Discord
from core import site_admin  # noqa: F401 – rejestracja działów i ustawień strony WWW


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
    list_display = ('name', 'email', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'email')
    fields = ('name', 'email', 'is_active', 'works_link')
    readonly_fields = ('works_link',)
    actions = ('merge_contacts',)

    @admin.action(description='Połącz zaznaczone profile w jeden (zostaje najstarszy)')
    def merge_contacts(self, request, queryset):
        from django.contrib import messages
        from django.db import IntegrityError, transaction
        from core.audiobook_merge import merge_audio_contributors
        from core.inflection import count_text
        try:
            with transaction.atomic():
                people = list(queryset.select_for_update().order_by('pk'))
                if len(people) < 2:
                    self.message_user(request, 'Zaznacz co najmniej dwa profile.', messages.ERROR)
                    return
                keep, *duplicates = people
                merge_audio_contributors(keep, duplicates)
        except IntegrityError:
            # Uzupełniony e-mail dałby parę (imię, e-mail) innego, niezaznaczonego wpisu.
            self.message_user(request, f'Nie połączono profili: „{keep.name}” z uzupełnionym e-mailem powielałby '
                              'inny wpis spisu. Zaznacz także ten wpis albo popraw go i spróbuj ponownie.', messages.ERROR)
            return
        count = len(duplicates) + 1
        self.message_user(request, f'Połączono {count_text(count, ("profil", "profile", "profili"))} '
                          f'w „{keep.name}”.', messages.SUCCESS)

    @admin.display(description='Nagrania')
    def works_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}">Zrealizowane nagrania</a>', reverse('core:audio_contributor', args=[obj.pk])) if obj.pk else 'Zapisz profil.'


@admin.register(AudiobookStage)
class AudiobookStageAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    """Korekta danych historycznych: daty i wykonawca. Rodzaj, kolejność i zakończenie zmienia proces."""
    list_display = ('text', 'stage_type', 'performer', 'queued_at', 'started_at', 'ended_at', 'is_completed')
    list_filter = ('stage_type', 'is_completed')
    search_fields = ('text__title',)
    list_select_related = ('text', 'performer')
    fields = ('text', 'stage_type', 'position', 'queued_at', 'started_at', 'start_unknown', 'ended_at', 'is_completed', 'performer')
    readonly_fields = ('text', 'stage_type', 'position', 'is_completed')

    def has_add_permission(self, request):
        return False

    def get_form(self, request, obj=None, **kwargs):
        from django import forms as django_forms
        from django.utils import timezone

        class StageForm(super().get_form(request, obj, **kwargs)):
            def clean(self):
                data = super().clean()
                today = timezone.localdate()
                for name in ('queued_at', 'started_at', 'ended_at'):
                    if data.get(name) and data[name] > today:
                        self.add_error(name, 'Data nie może być w przyszłości.')
                start, end = data.get('started_at'), data.get('ended_at')
                if start and end and end < start:
                    self.add_error('ended_at', 'Zakończenie nie może poprzedzać rozpoczęcia.')
                if end and not self.instance.is_completed:
                    raise django_forms.ValidationError('Etap trwa – zakończ go na stronie audiobooka.')
                return data
        return StageForm

    def save_model(self, request, obj, form, change):
        if obj.started_at:
            obj.start_unknown = False
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Audiobook)
class AudiobookAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = AudiobookAdminForm
    list_display = ('text', 'status', 'narrator_name', 'engineer_name', 'proofreader', 'premiere_date')
    list_filter = ('status', 'text__anthology')
    search_fields = ('text__title', 'narrator_name', 'engineer_name', 'proofreader__last_name')
    list_select_related = ('text__anthology', 'proofreader')
    autocomplete_fields = ('text', 'narrator_contact', 'engineer_contact')
    fieldsets = (
        ('Tekst i status', {'fields': ('text', 'status', 'production_link')}),
        ('Lektor i montaż (wpisy spisu)', {'fields': ('narrator_contact', 'engineer_contact')}),
        ('Korektor audiobooka', {'fields': ('proofreader',)}),
        ('Uwagi koordynatora', {'fields': ('coordinator_notes',)}),
        ('Publikacja', {'fields': ('premiere_date', 'youtube_url', 'hearthis_url', 'additional_links')}),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = ('status', 'production_link')
        if obj and not self.proofreader_editable(obj):
            # Jak na stronie: korektora zmienia się tylko w trakcie etapu Korekta, przed jego zakończeniem.
            fields = (*fields, 'proofreader')
        return (*fields, 'text') if obj else fields

    @staticmethod
    def proofreader_editable(obj):
        from core.selectors.audio_proofreading import can_assign_proofreader
        return can_assign_proofreader(obj)

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


class PublicGuidelinesAdminMixin:
    """Ustawienia strony zewnętrznej: ściąga składni przy polu wytycznych i odnośnik do strony."""
    public_page = ''  # nazwa trasy strony zewnętrznej
    readonly_fields = ('public_page_link',)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'guidelines':
            from core.public_guidelines import SYNTAX_HELP
            field.help_text = SYNTAX_HELP
            field.widget.attrs['rows'] = 20
        return field

    @admin.display(description='Strona zewnętrzna')
    def public_page_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">Zobacz na stronie</a>',
                           reverse(self.public_page))


@admin.register(PublicAudiobookSettings)
class PublicAudiobookSettingsAdmin(PublicGuidelinesAdminMixin, SingleRecordAdminMixin, SuperuserOnlyAdminMixin,
                                   admin.ModelAdmin):
    fields = ('mega_url', 'guidelines', 'public_page_link')
    public_page = 'core:external_audiobooks'



@admin.register(TextSocialMedia)
class TextSocialMediaAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('text', 'status', 'posted_on', 'url', 'updated_at')
    list_filter = ('status',)
    search_fields = ('text__title', 'url')
    list_select_related = ('text',)
    raw_id_fields = ('text',)
    readonly_fields = ('updated_at', 'updated_by')


@admin.register(SocialMediaEntry)
class SocialMediaEntryAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('description', 'status', 'posted_on', 'url', 'updated_at')
    list_filter = ('status',)
    search_fields = ('description', 'url')
    readonly_fields = ('created_at', 'updated_at', 'updated_by')


@admin.register(SocialMediaQuote)
class SocialMediaQuoteAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('__str__', 'added_by_name', 'posted_on', 'status', 'updated_at')
    list_filter = ('status',)
    search_fields = ('text__title', 'content', 'added_by_name')
    list_select_related = ('text',)
    raw_id_fields = ('text',)
    readonly_fields = ('added_by', 'added_by_name', 'created_at', 'updated_at', 'updated_by')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by, obj.added_by_name = request.user, person_name(request.user)[:200]
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(SocialCalendarEntry)
class SocialCalendarEntryAdmin(SocialMediaQuoteAdmin):
    """Wpisy kalendarza Social Media; dodanie w panelu nie wysyła powiadomienia Discord (tylko strona kalendarza)."""
    list_display = ('day', 'description', 'platform', 'added_by_name', 'updated_at')
    list_filter = ('platform',)
    search_fields = ('description', 'added_by_name')
    list_select_related = ()
    raw_id_fields = ()
    date_hierarchy = 'day'


User = get_user_model()


class IdentityFormMixin:
    """Login konta to e-mail: nazwa użytkownika powstaje z niego automatycznie, e-mail jest wspólny z osobą."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        self.fields["email"].help_text = "Jest też nazwą użytkownika i adresem kontaktowym osoby."

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_email(self):
        from people.accounts import validate_login_email
        from people.models import Person
        value = validate_login_email(
            self.cleaned_data["email"], exclude_pk=self.instance.pk,
            using=self.instance._state.db or "default",
        )
        others = Person.objects.filter(email__iexact=value)
        if self.instance.pk:
            others = others.exclude(user_id=self.instance.pk)
        other = others.first()
        if other:
            hint = "" if self.instance.pk else " – konto załóż na jej ekranie"
            raise forms.ValidationError(f"Ten adres e-mail ma już osoba „{other}”{hint}.")
        return value

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def _post_clean(self):
        email = self.cleaned_data.get("email")
        if email:
            self.instance.username = email
        super()._post_clean()


class AccountCreationForm(IdentityFormMixin, AdminUserCreationForm):
    send_invitation = forms.BooleanField(
        label="Wyślij zaproszenie e-mailem", required=False, initial=True,
        help_text="Osoba dostanie link do ustawienia własnego hasła. Pola hasła zostaw wtedy puste.")
    invite_discord = forms.BooleanField(label="Zaproś też na Discorda", required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Zamiast przełącznika Django „logowanie hasłem” – zaproszenie: hasło ustawia sama osoba.
        self.fields.pop("usable_password", None)
        for name in ("password1", "password2"):
            self.fields[name].help_text = "Tylko bez zaproszenia."
        from core.invitations import setup_discord_field
        setup_discord_field(self.fields["invite_discord"])

    def validate_passwords(self, *args, **kwargs):
        self.cleaned_data["usable_password"] = "false" if self.cleaned_data.get("send_invitation") else "true"
        super().validate_passwords(*args, **kwargs)


INVITATION_LOG = "Wysłano zaproszenie do CMS-u."


def invitation_state(user):
    """Data ostatniego zaproszenia (z dziennika admina) – wspólna dla ekranu konta i osoby."""
    from django.contrib.admin.models import LogEntry
    from django.utils import timezone
    last = LogEntry.objects.filter(object_id=str(user.pk), content_type__model="user",
                                   change_message=INVITATION_LOG).order_by("-action_time").first()
    if not last:
        return "Nie wysłano jeszcze zaproszenia."
    return f"Ostatnie zaproszenie: {timezone.localtime(last.action_time):%d.%m.%Y %H:%M}."


class AccountChangeForm(IdentityFormMixin, UserChangeForm):
    pass


class AccountAdmin(UserAdmin):
    """Konto logowania: hasło, superuser i zaproszenie. Role, e-mail i aktywność ustawia się na ekranie osoby."""
    list_display = ("username", "first_name", "last_name", "email", "is_active", "is_superuser")
    list_filter = ("is_superuser", "is_active")
    filter_horizontal = ()
    readonly_fields = (*UserAdmin.readonly_fields, "username", "profile_link", "invitation")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Dane osobowe", {"fields": ("first_name", "last_name", "email")}),
        ("Dostęp", {"fields": ("is_active", "is_superuser"),
                    "description": "Uprawnienia w CMS-ie wynikają z ról osoby. Do panelu administracyjnego wchodzi tylko superuser."}),
        ("Ważne daty", {"fields": ("last_login", "date_joined")}),
        ("Profil członka zespołu", {"fields": ("profile_link",)}),
        ("Zaproszenie do CMS-u", {"fields": ("invitation",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        # Logowanie osoby z ekipy włącza i wyłącza pole „wciąż w ekipie” na ekranie osoby.
        if obj and obj.pk and getattr(obj, "person_profile", None):
            fields = (*fields, "is_active")
        return fields

    @admin.display(description="Zaproszenie")
    def invitation(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        from core.invitations import discord_checkbox, invitation_days
        if not obj or not obj.pk:
            return "–"
        state = invitation_state(obj)
        if obj.last_login:
            state += f" Ostatnie logowanie: {obj.last_login:%d.%m.%Y}."
        return format_html(
            '<button type="submit" class="button" formaction="{}" formnovalidate name="_invite">Wyślij zaproszenie</button> {}'
            '<div class="help cms-inline-help">{} Mail z linkiem do ustawienia hasła (ważnym {} dni) trafi na adres {}. Niezapisane zmiany '
            'w formularzu nie zostaną zapisane.</div>',
            reverse("admin:auth_user_invite", args=[obj.pk]), discord_checkbox(), state, invitation_days(),
            obj.email or "– brak adresu –")

    def get_urls(self):
        from django.urls import path
        return [path("<int:object_id>/zaproszenie/", self.admin_site.admin_view(self.invite_view),
                     name="auth_user_invite")] + super().get_urls()

    def invite_view(self, request, object_id):
        from django.core.exceptions import PermissionDenied
        from django.http import HttpResponseNotAllowed
        from django.shortcuts import get_object_or_404, redirect
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        user = get_object_or_404(User, pk=object_id)
        if not self.has_change_permission(request, user):
            raise PermissionDenied
        from core.invitations import DISCORD_FIELD
        self._invite(request, user, discord=bool(request.POST.get(DISCORD_FIELD)))
        person = getattr(user, "person_profile", None) if request.POST.get("_from_person") else None
        if person:
            return redirect("admin:people_person_change", person.pk)
        return redirect("admin:auth_user_change", user.pk)

    def _invite(self, request, user, discord=False):
        """Zaproszenie wychodzi dopiero po zatwierdzeniu transakcji – wycofany zapis konta nie wysyła maila.
        discord – z linkiem zapraszającym na serwer Discorda (core.invitations)."""
        from django.db import transaction

        def send():
            from django.contrib import messages
            from django.core.exceptions import ValidationError
            from core.invitations import discord_invite_url, send_invitation
            try:
                send_invitation(user, request, discord=discord)
            except ValidationError as error:
                self.message_user(request, "Nie wysłano zaproszenia: " + " ".join(error.messages), messages.ERROR)
            except OSError:
                self.message_user(request, "Nie udało się wysłać wiadomości – sprawdź konfigurację poczty serwera.",
                                  messages.ERROR)
            else:
                self.log_change(request, user, INVITATION_LOG)
                note = " (z zaproszeniem na Discorda)" if discord and discord_invite_url() else ""
                self.message_user(request, f"Wysłano zaproszenie na adres {user.email}{note}.", messages.SUCCESS)

        transaction.on_commit(send)

    def response_add(self, request, obj, post_url_continue=None):
        form = getattr(request, "_account_add_form", None)
        if form is not None and form.cleaned_data.get("send_invitation"):
            self._invite(request, obj, discord=form.cleaned_data.get("invite_discord"))
        return super().response_add(request, obj, post_url_continue)

    def save_form(self, request, form, change):
        if not change:
            request._account_add_form = form
        return super().save_form(request, form, change)

    @admin.display(description="Profil osoby")
    def profile_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        person = getattr(obj, "person_profile", None) if obj and obj.pk else None
        if person is None:
            return "Brak powiązanego profilu"
        return format_html('<a href="{}">Otwórz profil: {}</a>', reverse('admin:people_person_change', args=[person.pk]), person)

    def save_model(self, request, obj, form, change):
        """Imiona i e-mail zmienione na koncie trafiają do osoby (e-mail osoby i konta są zawsze te same)."""
        obj._cms_edit_profile_name = True
        super().save_model(request, obj, form, change)
        person = getattr(obj, "person_profile", None)
        changed = {'first_name', 'last_name', 'email'}.intersection(form.changed_data)
        if person and changed:
            for name in changed:
                setattr(person, name, getattr(obj, name))
            person.save(update_fields=sorted(changed))

    search_fields = ('first_name__plcontains', 'last_name__plcontains', 'email__plcontains', 'username__plcontains')
    ordering = ('last_name', 'first_name', 'pk')
    form = AccountChangeForm
    add_form = AccountCreationForm
    add_fieldsets = ((None, {"classes": ("wide",), "fields": (
        "first_name", "last_name", "email", "send_invitation", "invite_discord", "password1", "password2",
    )}),)


if admin.site.is_registered(User):
    admin.site.unregister(User)
admin.site.register(User, AccountAdmin)

# Grupy kont nie dają uprawnień w CMS-ie (decydują role osoby), więc nie ma ich w panelu.
from django.contrib.auth.models import Group  # noqa: E402
if admin.site.is_registered(Group):
    admin.site.unregister(Group)



@admin.register(AnthologyCorrection)
class AnthologyCorrectionAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
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
class RecruitmentAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
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
        return obj.mail_roles_display

    @admin.display(description='Archiwalne decyzje usuniętych ról')
    def archived_decisions(self, obj):
        from core.selectors.recruitment import record_roles
        from django.utils.html import format_html_join
        if not obj or not obj.pk:
            return 'Brak'
        rows = list(obj.role_decisions.exclude(role__in=record_roles(obj.mail_roles)))
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
class ExtractAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
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
                if person_id := positive_id(request.POST.get("person")):
                    Person.objects.select_for_update().filter(pk=person_id).first()
            return super().changeform_view(request, object_id, form_url, extra_context)

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
        from django.core.exceptions import PermissionDenied
        from django.utils import timezone
        if obj.start_date <= timezone.localdate():
            raise PermissionDenied('Usunąć można tylko urlop, który jeszcze się nie rozpoczął.')
        obj.delete()


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
    # Etykieta wiersza to tytuł zgłoszenia – bez złączenia lista robiła zapytanie na wiersz.
    list_select_related = ('review',)




@admin.register(UserActivity)
class UserActivityAdmin(ReadOnlyAdminMixin, SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action', 'target', 'method', 'status_code')
    list_filter = ('method', 'created_at')
    search_fields = ('actor', 'action', 'target')
    readonly_fields = tuple(field.name for field in UserActivity._meta.fields)
    actions = None



@admin.register(WorkflowEvent)
class WorkflowEventAdmin(ReadOnlyAdminMixin, SuperuserOnlyAdminMixin, admin.ModelAdmin):
    """Dziennik zdarzeń: tylko do odczytu, wyłącznie dla superusera."""
    list_display = ('created_at', 'title', 'previous_status', 'next_status', 'actor_name')
    list_filter = ('kind',)
    search_fields = ('title', 'actor_name')
    fields = readonly_fields = ('created_at', 'kind', 'text', 'title', 'authors', 'actor', 'actor_name',
                                'previous_status', 'next_status', 'details', 'personal_work', 'personal_work_description')







class MailboxConnectionForm(forms.ModelForm):
    password = forms.CharField(label='Hasło / hasło aplikacji', required=False,
        strip=False, widget=forms.PasswordInput(render_value=False),
        help_text='Pozostaw puste, aby zachować zapisane hasło. Hasło nie jest wyświetlane po zapisaniu.')

    class Meta:
        model = MailboxConnection
        fields = ('name', 'purpose', 'username', 'password', 'is_active',
                  *MailboxConnection.IMAP_FIELDS, *MailboxConnection.SMTP_FIELDS)

    # Pola z wartością domyślną: puste znaczy „domyślne” (pole drugiego protokołu bywa ukryte i niewypełnione).
    DEFAULTED = ('port', 'security', 'folder', 'smtp_port', 'smtp_security')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.DEFAULTED:
            self.fields[name].required = False

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        if not password and not self.instance.encrypted_password:
            raise forms.ValidationError('Podaj hasło skrzynki lub hasło aplikacji.')
        return password

    def clean(self):
        data = super().clean()
        for name in self.DEFAULTED:
            if name in data and data[name] in (None, ''):
                data[name] = MailboxConnection._meta.get_field(name).get_default()
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
class MailboxConnectionAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = MailboxConnectionForm
    list_display = ('name', 'purpose', 'server', 'username', 'folder', 'is_active')
    list_filter = ('purpose', 'is_active')
    fields = ('name', 'purpose', 'username', 'password', 'is_active',
              *MailboxConnection.IMAP_FIELDS, *MailboxConnection.SMTP_FIELDS)
    search_fields = ('name', 'host', 'smtp_host', 'username')

    class Media:
        # Pokazuje tylko sekcję pasującą do wybranego przeznaczenia (IMAP albo SMTP).
        js = ('core/mailbox-admin.js',)

    @admin.display(description='serwer', ordering='host')
    def server(self, obj):
        return obj.smtp_host if obj.is_sender else obj.host

    def get_fieldsets(self, request, obj=None):
        fields = self.get_fields(request, obj)
        imap, smtp = MailboxConnection.IMAP_FIELDS, MailboxConnection.SMTP_FIELDS
        return [(None, {'fields': [name for name in fields if name not in imap + smtp],
                        'description': 'Login to zwykle pełny adres e-mail skrzynki; przy wysyłce plików autorom musi nim być '
                                       '(to adres nadawcy). Pozostałe pola zależą od przeznaczenia.'}),
                ('Odbiór poczty (IMAP)', {
                    'fields': [name for name in fields if name in imap], 'classes': ('mailbox-imap',),
                    'description': 'Dla zgłoszeń tekstów i rekrutacji. Skrzynka „Wysyłka plików autorom” '
                                   'nie odbiera poczty – tych pól nie wypełnia się.'}),
                ('Wysyłka plików autorom (SMTP)', {
                    'fields': [name for name in fields if name in smtp], 'classes': ('mailbox-smtp',),
                    'description': 'Tylko dla przeznaczenia „Wysyłka plików autorom”: z tej skrzynki CMS wysyła '
                                   'autorom ich pliki po podziale dokumentu nagłówkami (Programy → Podział DOCX). '
                                   'Login (adres nadawcy) i hasło jak wyżej.'})]

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


from core.models import MailboxHeaderSnapshot  # noqa: E402


@admin.register(MailboxHeaderSnapshot)
class MailboxHeaderSnapshotAdmin(ReadOnlyAdminMixin, SuperuserOnlyAdminMixin, admin.ModelAdmin):
    """Wynik nocnego pobierania nagłówków – tylko do odczytu (odświeża go komenda fetch_mailbox_headers)."""
    list_display = ('mailbox', 'fetched_at', 'header_count', 'error')
    fields = ('mailbox', 'fetched_at', 'sent_since', 'header_count', 'error')
    readonly_fields = fields

    @admin.display(description='nagłówki')
    def header_count(self, obj):
        return len(obj.result.get('rows', []))


from core.models import ReplacementEntry  # noqa: E402
from core.odkurzacz_forms import ReplacementEntryForm  # noqa: E402


class ReplacementEntryAdminForm(ReplacementEntryForm):
    """Panel jest dostępny tylko dla superusera, więc dopuszcza wyrażenia regularne."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_patterns', True)
        super().__init__(*args, **kwargs)


@admin.register(ReplacementEntry)
class ReplacementEntryAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = ReplacementEntryAdminForm
    list_display = ('position', 'kind', 'source', 'target', 'is_active', 'note')
    list_display_links = ('source',)
    list_filter = ('kind', 'is_active')
    search_fields = ('source', 'target', 'note')
    ordering = ('position', 'pk')


from core.audio_description_admin import AudioDescriptionAdmin  # noqa: E402,F401
