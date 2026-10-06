from django import forms
from django.contrib import admin
from django.db.models import Value
from django.db.models.functions import Lower

from .models import Person, Role, format_local_datetime


class PersonAdminForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = (
            "first_name",
            "last_name",
            "email",
            "dropbox_email",
            "previous_data",
            "is_active",
            "roles",
            "is_coordinator",
            "user",
            "author_profile",
            "leave_start_date",
            "leave_end_date",
            "leave_until_revoked",
        )

    def clean(self):
        data = super().clean()
        if data.get('user') and data['user'].is_active and not data.get('email') and 'email' not in self.errors:
            self.add_error('email', 'Aktywne konto wymaga adresu e-mail.')
        if self.instance.pk and self.instance.is_coordinator and not data.get('is_coordinator') and data.get('roles') is not None:
            from people.coordinator_access import coordinator_query
            data['roles'] = data['roles'].exclude(coordinator_query())
        return data

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if not email:
            if self.instance.user_id and self.instance.user.is_active:
                raise forms.ValidationError("Aktywne konto wymaga adresu e-mail.")
            return None

        # Sprawdzenie obejmuje również osoby, które opuściły zespół.
        # LOWER po obu stronach zapewnia porównanie bez względu na wielkość
        # liter również przy kolacji MySQL rozróżniającej wielkość liter.
        queryset = Person.objects.alias(
            email_lower=Lower("email"),
        ).filter(email_lower=Lower(Value(email)))

        if self.instance.pk is not None:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise forms.ValidationError(
                "Osoba z tym adresem e-mail już istnieje. "
                "Sprawdź również osoby nieoznaczone jako "
                "„wciąż w ekipie”.",
                code="duplicate_email",
            )

        return email

    def clean_dropbox_email(self):
        return self.cleaned_data["dropbox_email"].strip()


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return ("name",) if obj else ()

    list_display = (
        "name",
    )

    search_fields = (
        "name__plcontains",
    )

    ordering = (
        "name",
        "pk",
    )

    list_per_page = 50


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    change_form_template = "admin/people/person/change_form.html"

    readonly_fields = ("account_link", "account_date_joined", "account_last_login",
                       "leave_start_date", "leave_end_date", "leave_until_revoked")

    class Media:
        css = {"all": ("core/admin-person.css",)}

    def get_readonly_fields(self, request, obj=None):
        return (*self.readonly_fields, *(("user",) if obj and obj.user_id else ()))

    actions = ('revoke_coordinator_access',)

    @admin.action(description="Odbierz wszystkie uprawnienia koordynatora")
    def revoke_coordinator_access(self, request, queryset):
        from people.coordinator_access import revoke_coordinator
        from django.core.exceptions import PermissionDenied
        if not request.user.is_superuser:
            raise PermissionDenied()
        for person in queryset.order_by('user_id', 'pk'):
            revoke_coordinator(person)
        self.message_user(request, "Usunięto funkcje koordynatora, grupy koordynatorskie i oznaczenie koordynatora. Superuserzy zachowują swoje uprawnienia.")

    form = PersonAdminForm

    list_display = (
        "full_name",
        "display_roles",
        "is_active",
        "is_coordinator",
        "email",
        "dropbox_email",
    )

    list_display_links = (
        "full_name",
    )

    list_filter = (
        "is_active",
        "roles",
        "is_coordinator",
    )

    search_fields = (
        "first_name__plcontains",
        "last_name__plcontains",
        "email__plcontains",
        "dropbox_email__plcontains",
    )

    ordering = (
        "last_name",
        "first_name",
        "pk",
    )

    filter_horizontal = (
        "roles",
    )

    autocomplete_fields = (
        "user", "author_profile",
    )

    list_per_page = 50
    show_full_result_count = False

    fieldsets = (
        ('Dane osoby i kontakt', {'fields': ('first_name', 'last_name', 'email', 'dropbox_email')}),
        ('Role i aktywność', {'fields': ('is_active', 'roles', 'is_coordinator'),
            'description': 'Wyłączenie aktywności ukrywa osobę na liście zespołu. Profil i dawne przydziały pozostają.'}),
        ('Powiązane konto i profil autora', {'fields': ('user', 'account_link', 'author_profile')}),
        ('Urlop', {'classes': ('person-account-half',),
                  'fields': ('leave_start_date', 'leave_end_date', 'leave_until_revoked')}),
        ('Aktywność konta', {'classes': ('person-account-half',),
                            'fields': ('account_date_joined', 'account_last_login'),
                            'description': 'Daty dotyczą powiązanego konta użytkownika.'}),
        ('Pozostałe informacje', {'classes': ('collapse',), 'fields': ('previous_data',)}),
    )

    @admin.display(description="Konto użytkownika")
    def account_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        if not obj or not obj.user_id:
            return "Brak powiązanego konta"
        return format_html('<a href="{}">Otwórz konto</a>', reverse('admin:auth_user_change', args=[obj.user_id]))

    @admin.display(description="Data dołączenia")
    def account_date_joined(self, obj):
        if not obj or not obj.user_id:
            return "Brak powiązanego konta"
        return format_local_datetime(obj.user.date_joined)

    @admin.display(description="Ostatnie logowanie")
    def account_last_login(self, obj):
        if not obj or not obj.user_id:
            return "Brak powiązanego konta"
        if not obj.user.last_login:
            return "Jeszcze się nie logowano"
        return format_local_datetime(obj.user.last_login)

    def get_queryset(self, request):
        # Admin zachowuje dostęp do byłych członków zespołu.
        # Filtrowanie aktywnych osób odbywa się przez list_filter.
        return (
            super()
            .get_queryset(request)
            .select_related("user")
            .prefetch_related("roles")
        )

    @admin.display(
        description="imię i nazwisko",
        ordering="last_name",
    )
    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

    @admin.display(description="role")
    def display_roles(self, obj):
        return ", ".join(
            role.name
            for role in obj.roles.all()
        ) or "–"


# Urlopy rejestrowane są w core.admin. Jedyny model rekrutacji to core.Recruitment.
