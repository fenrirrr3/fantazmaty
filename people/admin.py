from django import forms
from django.contrib import admin
from django.db.models import Value
from django.db.models.functions import Lower

from .models import Person, Role, format_local_datetime


class PersonAdminForm(forms.ModelForm):
    create_account = forms.BooleanField(
        label="Utwórz konto logowania i wyślij zaproszenie", required=False,
        help_text="Loginem będzie adres e-mail osoby. Osoba ustawi hasło z linku w mailu.")

    class Meta:
        model = Person
        fields = (
            "first_name",
            "last_name",
            "email",
            "dropbox_email",
            "previous_data",
            "is_active",
            "is_external",
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
        if data.get('is_external'):
            data['is_active'] = False
        if data.get('is_coordinator') and not self.instance.is_coordinator:
            raise forms.ValidationError('Nadaj koordynatora konkretnego działu w polu Role.')
        if data.get('user') and data['user'].is_active and not data.get('is_external') and not data.get('email') and 'email' not in self.errors:
            self.add_error('email', 'Aktywne konto wymaga adresu e-mail.')
        if self.instance.pk and self.instance.is_coordinator and not data.get('is_coordinator') and data.get('roles') is not None:
            from people.roles import coordinator_role_q
            data['roles'] = data['roles'].exclude(coordinator_role_q())
        self._clean_account(data)
        return data

    def _clean_account(self, data):
        """E-mail osoby jest loginem konta – musi być wolny w innych kontach; konto zakłada się tylko osobie z ekipy."""
        from django.core.exceptions import ValidationError
        from people.accounts import normalize_email, validate_login_email
        account = data.get('user') if 'user' in self.fields else self.instance.user
        email = normalize_email(data.get('email'))
        if data.get('create_account'):
            if account:
                return self.add_error('create_account', 'Osoba ma już konto – nie zaznaczaj tworzenia nowego.')
            if data.get('is_external'):
                return self.add_error('create_account', 'Zewnętrzny nie loguje się do CMS-u – nie można założyć mu konta.')
            if not data.get('is_active'):
                return self.add_error('create_account', 'Konto można założyć tylko osobie „wciąż w ekipie”.')
            if not email and 'email' not in self.errors:
                return self.add_error('email', 'Konto wymaga adresu e-mail – to on będzie loginem.')
        elif not account or (account.email, account.username) == (email, email):
            return
        if email and 'email' not in self.errors:
            try:
                validate_login_email(email, exclude_pk=account.pk if account else None)
            except ValidationError as error:
                self.add_error('email', error)

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if not email:
            if self.instance.user_id and self.instance.user.is_active and not (self.data.get('is_external') or self.instance.is_external):
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

    readonly_fields = ("account_link", "account_status", "account_date_joined", "account_last_login",
                       "leave_start_date", "leave_end_date", "leave_until_revoked")

    class Media:
        css = {"all": ("core/admin-person.css",)}

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
        "is_external",
        "is_coordinator",
        "email",
        "dropbox_email",
    )

    list_display_links = (
        "full_name",
    )

    list_filter = (
        "is_active",
        "is_external",
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
        ('Role i aktywność', {'fields': ('is_active', 'is_external', 'roles', 'is_coordinator'),
            'description': 'Zewnętrzny pozostaje na liście zespołu, ale nie loguje się i nie jest liczony w aktywności. Koordynację nadaj przez rolę działową.'}),
        ('Powiązane konto i profil autora', {'fields': ('user', 'create_account', 'account_link', 'account_status', 'author_profile'),
            'description': 'E-mail osoby jest loginem konta, a odznaczenie „wciąż w ekipie” blokuje logowanie.'}),
        ('Urlop', {'classes': ('person-account-half',),
                  'fields': ('leave_start_date', 'leave_end_date', 'leave_until_revoked')}),
        ('Aktywność konta', {'classes': ('person-account-half',),
                            'fields': ('account_date_joined', 'account_last_login'),
                            'description': 'Daty dotyczą powiązanego konta użytkownika.'}),
        ('Pozostałe informacje', {'classes': ('collapse',), 'fields': ('previous_data',)}),
    )

    def get_fieldsets(self, request, obj=None):
        """Z kontem: login z odnośnikiem i stan konta. Bez konta: powiązanie istniejącego konta albo utworzenie nowego."""
        hidden = {'user', 'create_account'} if obj and obj.user_id else {'account_link'}
        return [(name, {**options, 'fields': tuple(f for f in options['fields'] if f not in hidden)})
                for name, options in self.fieldsets]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get('user')
        if field is not None:
            # Konto powstaje z ekranu osoby (pole „Utwórz konto…”), więc bez przycisków dodawania i edycji konta obok listy.
            field.label = 'Powiąż istniejące konto'
            for name in ('can_add_related', 'can_change_related', 'can_delete_related', 'can_view_related'):
                setattr(field.widget, name, False)
        return form

    def save_model(self, request, obj, form, change):
        """Konto z ekranu osoby: login = e-mail osoby, hasło ustawia sama osoba z zaproszenia (wysyłanego po commit)."""
        from django.contrib.auth import get_user_model
        from people.accounts import normalize_email
        User = get_user_model()
        created = None
        if form.cleaned_data.get('create_account') and not obj.user_id:
            email = normalize_email(obj.email)
            created = User.objects.create_user(username=email, email=email,
                                               first_name=obj.first_name, last_name=obj.last_name)
            obj.user = created
        super().save_model(request, obj, form, change)
        if created:
            self.admin_site._registry[User]._invite(request, created)

    @admin.display(description="Stan konta")
    def account_status(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        from core.admin import invitation_state
        if not obj or not obj.user_id:
            return "Brak konta – osoba nie loguje się do CMS-u."
        user = obj.user
        login = "Logowanie aktywne." if user.is_active else "Logowanie zablokowane."
        password = ("Hasło ustawione." if user.has_usable_password()
                    else "Zaproszenie nieprzyjęte – hasło nieustawione.")
        return format_html(
            '{} {} {}<br><button type="submit" class="button" formaction="{}" formnovalidate name="_from_person" value="1">'
            'Wyślij zaproszenie</button><div class="help">Zaproszenie trafi na adres {}. Niezapisane zmiany w formularzu '
            'nie zostaną zapisane.</div>',
            login, password, invitation_state(user), reverse('admin:auth_user_invite', args=[user.pk]),
            user.email or '– brak adresu –')

    @admin.display(description="Login")
    def account_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html
        if not obj or not obj.user_id:
            return "Brak powiązanego konta"
        return format_html('{} – <a href="{}">otwórz konto</a>', obj.user.username,
                           reverse('admin:auth_user_change', args=[obj.user_id]))

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
