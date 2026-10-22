from django import forms
from django.contrib import admin
from django.db.models import Value
from django.db.models.functions import Lower

from .models import DropboxAccess, DropboxFolder, Person, Role, format_local_datetime

DROPBOX_FIELD = "dropbox_folder_"
DROPBOX_CHOICES = (("", "Brak dostępu"), *DropboxAccess.Level.choices)

ACCOUNT_CONFLICT = "Konto z tym adresem e-mail założono przed chwilą (np. w innej karcie). Sprawdź dane i zapisz ponownie."


class AccountConflict(Exception):
    """Konto z e-mailem osoby powstało równolegle – zapis osoby jest wycofywany, formularz pokazuje błąd pola."""


class PersonAdminForm(forms.ModelForm):
    # Ustawiane przez PersonAdmin po wycofanym zapisie (wyścig przy zakładaniu konta).
    account_conflict = False

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._add_dropbox_fields()

    def _add_dropbox_fields(self):
        """Pole wyboru poziomu dla każdego folderu z listy (people.dropbox_access.folders_for)."""
        from people.dropbox_access import folders_for
        person = self.instance if self.instance.pk else None
        rows = {access.folder_id: access for access in person.dropbox_accesses.all()} if person else {}
        self.dropbox_folders = {}
        for folder in folders_for(person):
            name = f"{DROPBOX_FIELD}{folder.pk}"
            access = rows.get(folder.pk)
            self.fields[name] = forms.ChoiceField(
                label=f"Dropbox: {folder.name}", choices=DROPBOX_CHOICES, required=False,
                initial=access.level if access and access.wanted else "",
                help_text=dropbox_state(access, folder))
            self.dropbox_folders[name] = folder

    def dropbox_levels(self):
        """{folder: poziom albo ''} z wysłanego formularza. Pole nieobecne w danych (inny formularz) – bez zmian;
        osoba spoza zespołu – nic nie jest nadawane (dostępy odbiera dezaktywacja)."""
        data = self.cleaned_data
        if not data.get('is_active') or data.get('is_external'):
            return {}
        return {folder: data.get(name, '') for name, folder in self.dropbox_folders.items() if name in self.data}

    def _clean_dropbox(self, data):
        chosen = [name for name, folder in self.dropbox_folders.items() if name in self.data and data.get(name)]
        if not chosen:
            return
        if not data.get('is_active') or data.get('is_external'):
            for name in chosen:
                if name in self.changed_data:
                    self.add_error(name, 'Dostęp do folderów mają tylko osoby „wciąż w ekipie”.')
            return
        if not data.get('dropbox_email') and 'dropbox_email' not in self.errors:
            self.add_error('dropbox_email', 'Podaj adres e-mail Dropboxa – bez niego nie można nadać dostępu do folderów.')

    def clean(self):
        data = super().clean()
        if data.get('is_external'):
            data['is_active'] = False
        self._clean_dropbox(data)
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
        if data.get('create_account') and self.account_conflict:
            return self.add_error('email', ACCOUNT_CONFLICT)
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


def dropbox_state(access, folder):
    """Opis stanu dostępu pod polem folderu na ekranie osoby."""
    if access is None:
        return f"Folder {folder.path}. Dostęp nie był nadawany."
    text = f"Folder {folder.path}. Stan: {access.get_state_display()}"
    if access.granted_email:
        text += f" ({access.granted_email})"
    if access.synced_at:
        text += f", synchronizacja {format_local_datetime(access.synced_at)}"
    text += "."
    if access.last_error:
        text += f" Ostatni błąd: {access.last_error}"
    return text


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

    actions = ('revoke_coordinator_access', 'remove_from_team', 'sync_dropbox_access')

    @admin.action(description="Usuń z zespołu (odznacz „wciąż w ekipie”, odbierz dostęp do Dropboxa)")
    def remove_from_team(self, request, queryset):
        """Jak odznaczenie „wciąż w ekipie” na ekranie osoby: blokada logowania i odebranie folderów Dropbox."""
        from django.db import transaction
        from people.dropbox_access import admin_messages, report_to
        with transaction.atomic():
            report_to(request, admin_messages(self, request))
            people = list(queryset.select_for_update().filter(is_active=True).order_by('pk'))
            for person in people:
                person.is_active = False
                person.save(update_fields=['is_active'])
        self.message_user(request, f"Usunięto z zespołu: {len(people)}.")

    @admin.action(description="Synchronizuj ponownie dostęp do folderów Dropbox")
    def sync_dropbox_access(self, request, queryset):
        sync_dropbox_rows(self, request, DropboxAccess.objects.filter(person__in=queryset))

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
        """Z kontem: login z odnośnikiem i stan konta. Bez konta: powiązanie istniejącego konta albo utworzenie nowego.
        Za kontem – poziom dostępu do folderów Dropbox (po jednym polu na folder)."""
        hidden = {'user', 'create_account'} if obj and obj.user_id else {'account_link'}
        fieldsets = [(name, {**options, 'fields': tuple(f for f in options['fields'] if f not in hidden)})
                     for name, options in self.fieldsets]
        index = next(i for i, (name, _) in enumerate(fieldsets) if name.startswith('Powiązane konto')) + 1
        fieldsets.insert(index, ('Dostęp do folderów Dropbox', {
            'fields': tuple(f"{DROPBOX_FIELD}{folder.pk}" for folder in self.dropbox_folders(obj)),
            'description': dropbox_description(),
        }))
        return fieldsets

    @staticmethod
    def dropbox_folders(obj):
        from people.dropbox_access import folders_for
        return folders_for(obj)

    def get_form(self, request, obj=None, **kwargs):
        # Pola folderów dodaje sam formularz (lista zależy od bazy), więc nie trafiają do modelform_factory.
        from django.contrib.admin.utils import flatten_fieldsets
        fields = kwargs['fields'] if kwargs.get('fields') is not None else flatten_fieldsets(self.get_fieldsets(request, obj))
        kwargs['fields'] = [name for name in fields if not name.startswith(DROPBOX_FIELD)]
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get('user')
        if field is not None:
            # Konto powstaje z ekranu osoby (pole „Utwórz konto…”), więc bez przycisków dodawania i edycji konta obok listy.
            field.label = 'Powiąż istniejące konto'
            for name in ('can_add_related', 'can_change_related', 'can_delete_related', 'can_view_related'):
                setattr(field.widget, name, False)
        if getattr(request, '_account_conflict', False):
            form = type(form.__name__, (form,), {'account_conflict': True})
        return form

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        """Wyścig przy zakładaniu konta: transakcja zapisu jest wycofana (bez osoby, konta i zaproszenia),
        a formularz wraca z błędem pola e-mail zamiast błędu 500."""
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except AccountConflict:
            request._account_conflict = True
            return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        """Konto z ekranu osoby: login = e-mail osoby, hasło ustawia sama osoba z zaproszenia (wysyłanego po commit)."""
        from django.contrib.auth import get_user_model
        from people.accounts import normalize_email
        from people.dropbox_access import admin_messages, report_to
        User = get_user_model()
        # Wyniki synchronizacji Dropboxa (po zatwierdzeniu zapisu) trafią do komunikatów tej strony.
        report_to(request, admin_messages(self, request))
        created = None
        if form.cleaned_data.get('create_account') and not obj.user_id:
            email = normalize_email(obj.email)
            from django.db import IntegrityError, transaction
            try:
                with transaction.atomic():
                    created = User.objects.create_user(username=email, email=email,
                                                       first_name=obj.first_name, last_name=obj.last_name)
            except IntegrityError:
                # Konto z tym adresem powstało po walidacji formularza (druga karta, inny administrator).
                raise AccountConflict() from None
            obj.user = created
        super().save_model(request, obj, form, change)
        if created:
            self.admin_site._registry[User]._invite(request, created)

    def save_related(self, request, form, formsets, change):
        from people.dropbox_access import set_person_access
        super().save_related(request, form, formsets, change)
        set_person_access(form.instance, form.dropbox_levels())

    def delete_model(self, request, obj):
        from people.dropbox_access import admin_messages, report_to
        report_to(request, admin_messages(self, request))
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        """Usunięcie osób odbiera ich dostęp do folderów Dropbox (wiersze zostają do czasu usunięcia adresu)."""
        from django.db import transaction
        from people.dropbox_access import admin_messages, report_to
        with transaction.atomic():
            report_to(request, admin_messages(self, request))
            super().delete_queryset(request, queryset)

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


def dropbox_description():
    from django.urls import reverse
    from django.utils.html import format_html
    from people.dropbox_access import NOT_CONFIGURED_NOTICE
    from people.dropbox_sharing import is_configured
    text = ('Poziom „Edycja” albo „Odczyt” wymaga adresu e-mail Dropboxa. Osoba bez konta Dropbox dostanie '
            'zaproszenie e-mailem. Zmiana adresu przenosi dostęp na nowy adres, a odznaczenie „wciąż w ekipie” '
            'odbiera wszystkie dostępy (ponowne zaznaczenie ich nie przywraca).')
    if not is_configured():
        text = f'{NOT_CONFIGURED_NOTICE} {text}'
    return format_html('{} <a href="{}">Lista folderów</a>.', text, reverse('admin:people_dropboxfolder_changelist'))


def sync_dropbox_rows(model_admin, request, queryset):
    """Akcja „Synchronizuj ponownie”: od razu, z komunikatami wyników."""
    from django.contrib import messages
    from people.dropbox_access import NOT_CONFIGURED_NOTICE, sync
    results = sync(queryset, actor=request.user)
    if not results:
        model_admin.message_user(request, "Brak dostępów do synchronizacji.", messages.WARNING)
    elif any(result.skipped for result in results):
        model_admin.message_user(request, NOT_CONFIGURED_NOTICE, messages.WARNING)
        return
    for result in results:
        level = messages.ERROR if result.error else messages.SUCCESS
        model_admin.message_user(request, result.describe(), level)


@admin.register(DropboxFolder)
class DropboxFolderAdmin(admin.ModelAdmin):
    """Foldery, którymi zarządza CMS; dodawane ręcznie albo z listy podfolderów folderu głównego."""
    change_list_template = "admin/people/dropboxfolder/change_list.html"
    change_form_template = "admin/people/dropboxfolder/change_form.html"
    list_display = ("name", "path", "is_shared", "is_active", "access_count")
    actions = ("import_members", "remove_from_list")
    list_filter = ("is_active",)
    search_fields = ("name__plcontains", "path__plcontains")
    fields = ("name", "path", "is_active", "shared_folder_id")
    readonly_fields = ("shared_folder_id",)

    @admin.display(description="udostępniony", boolean=True)
    def is_shared(self, obj):
        return bool(obj.shared_folder_id)

    @admin.display(description="osoby z dostępem")
    def access_count(self, obj):
        return obj.accesses.filter(wanted=True).count()

    def get_urls(self):
        from django.urls import path
        return [path("pobierz/", self.admin_site.admin_view(self.fetch_view), name="people_dropboxfolder_fetch"),
                path("przejmij/", self.admin_site.admin_view(self.import_view), name="people_dropboxfolder_import"),
                path("usun/", self.admin_site.admin_view(self.remove_view), name="people_dropboxfolder_remove"),
                *super().get_urls()]

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("import_members", None)
            actions.pop("remove_from_list", None)
        return actions

    @admin.action(description="Usuń z listy (po sprawdzeniu stanu w Dropboxie)")
    def remove_from_list(self, request, queryset):
        from django.shortcuts import redirect
        from django.urls import reverse
        ids = ",".join(str(pk) for pk in queryset.order_by("pk").values_list("pk", flat=True))
        return redirect(f"{reverse('admin:people_dropboxfolder_remove')}?ids={ids}")

    def remove_view(self, request):
        """Usunięcie folderów z listy: sprawdzenie stanu w Dropboxie i wybór (podgląd), potem usunięcie tego samego
        stanu (skrót w tokenie) – people.dropbox_removal."""
        from django.conf import settings
        from django.contrib import messages
        from django.core import signing
        from django.core.exceptions import PermissionDenied
        from django.shortcuts import redirect
        from django.template.response import TemplateResponse
        from people import dropbox_import, dropbox_removal
        if not request.user.is_superuser:
            raise PermissionDenied
        source = request.POST if request.method == "POST" else request.GET
        folders = dropbox_import.folders_by_ids(source.get("ids", "").split(","))
        if not folders:
            self.message_user(request, "Nie wybrano folderów.", messages.WARNING)
            return redirect("admin:people_dropboxfolder_changelist")
        salt = "people.dropbox_removal"
        checks = dropbox_removal.check_folders(folders)
        if request.method == "POST":
            try:
                expected = signing.loads(request.POST.get("token", ""), salt=salt, max_age=settings.SESSION_COOKIE_AGE)
            except signing.BadSignature:
                expected = ""
            if expected != dropbox_removal.checks_digest(checks):
                self.message_user(request, "Stan folderów zmienił się od sprawdzenia – sprawdź go ponownie i potwierdź.",
                                  messages.WARNING)
            else:
                for check in checks:
                    mode = request.POST.get(f"mode_{check.folder.pk}", "")
                    removed, failed = dropbox_removal.remove(check, mode, actor=request.user)
                    name = check.folder.name
                    if removed:
                        self.message_user(request, f"Usunięto folder {name} z listy ({check.label.lower()}).",
                                          messages.SUCCESS)
                    elif failed or (mode == dropbox_removal.REVOKE and check.state == dropbox_removal.EXISTS):
                        self.message_user(request, f"Folder {name} został na liście – nie udało się odebrać "
                                          "dostępu wszystkim osobom.", messages.ERROR)
                        for result in failed:
                            self.message_user(request, result.describe(), messages.ERROR)
                    else:
                        self.message_user(request, f"Folder {name} został na liście – nie wybrano, co zrobić "
                                          "z dostępem.", messages.WARNING)
                return redirect("admin:people_dropboxfolder_changelist")
        return TemplateResponse(request, "admin/people/dropboxfolder/remove.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta, "title": "Usuń foldery z listy",
            "checks": checks, "ids": ",".join(str(folder.pk) for folder in folders),
            "token": signing.dumps(dropbox_removal.checks_digest(checks), salt=salt),
        })

    @admin.action(description="Przejmij obecnych członków z Dropboxa")
    def import_members(self, request, queryset):
        from django.shortcuts import redirect
        from django.urls import reverse
        ids = ",".join(str(pk) for pk in queryset.order_by("pk").values_list("pk", flat=True))
        return redirect(f"{reverse('admin:people_dropboxfolder_import')}?ids={ids}")

    def import_view(self, request):
        """Przejęcie członków folderów z Dropboxa: podgląd bez zapisu, „Zastosuj” – ten sam plan (skrót w tokenie)."""
        from django.conf import settings
        from django.contrib import messages
        from django.core import signing
        from django.core.exceptions import PermissionDenied
        from django.shortcuts import redirect
        from django.template.response import TemplateResponse
        from people import dropbox_import
        from people.dropbox_access import admin_messages
        from people.dropbox_sharing import DropboxError
        if not request.user.is_superuser:
            raise PermissionDenied
        source = request.POST if request.method == "POST" else request.GET
        folders = dropbox_import.folders_by_ids(source.get("ids", "").split(","))
        if not folders:
            self.message_user(request, "Nie wybrano folderów.", messages.WARNING)
            return redirect("admin:people_dropboxfolder_changelist")
        salt = "people.dropbox_import"
        error, plans = "", []
        try:
            plans = dropbox_import.plan_folders(folders)
        except DropboxError as exception:
            error = str(exception)
        if request.method == "POST" and not error:
            try:
                expected = signing.loads(request.POST.get("token", ""), salt=salt, max_age=settings.SESSION_COOKIE_AGE)
            except signing.BadSignature:
                expected = ""
            plans, counts = dropbox_import.apply_plans(
                plans, set_email=request.POST.getlist("set_email"), revoke=request.POST.getlist("revoke"),
                actor=request.user, request=request, notify=admin_messages(self, request), expected=expected)
            if counts is None:
                self.message_user(request, "Stan w Dropboxie albo w CMS-ie zmienił się od podglądu – sprawdź nowy "
                                  "podgląd i zastosuj ponownie.", messages.WARNING)
            else:
                for plan in plans:
                    level = messages.ERROR if plan.error else messages.SUCCESS
                    self.message_user(request, dropbox_import.summary(plan, counts[plan.folder.pk]), level)
                return redirect("admin:people_dropboxfolder_changelist")
        return TemplateResponse(request, "admin/people/dropboxfolder/import.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta,
            "title": "Przejmij obecnych członków z Dropboxa", "error": error, "plans": plans,
            "ids": ",".join(str(folder.pk) for folder in folders),
            "token": signing.dumps(dropbox_import.plans_digest(plans), salt=salt),
        })

    def fetch_view(self, request):
        """Podfoldery folderu głównego z Dropboxa; zaznaczone trafiają na listę folderów CMS-u."""
        from django.conf import settings
        from django.contrib import messages
        from django.core.exceptions import PermissionDenied
        from django.shortcuts import redirect
        from django.template.response import TemplateResponse
        from people.dropbox_removal import missing_in_listing
        from people.dropbox_sharing import DropboxClient, DropboxError
        if not self.has_add_permission(request):
            raise PermissionDenied
        root = settings.DROPBOX_ROOT_FOLDER
        known = {path.casefold() for path in DropboxFolder.objects.values_list("path", flat=True)}
        try:
            folders = DropboxClient().list_subfolders(root)
        except DropboxError as error:
            folders, error_message = [], str(error)
        else:
            error_message = ""
        for folder in folders:
            folder["known"] = folder["path"].casefold() in known
        if request.method == "POST" and not error_message:
            chosen = set(request.POST.getlist("path"))
            added = [DropboxFolder.objects.create(name=folder["name"], path=folder["path"],
                                                  shared_folder_id=folder["shared_folder_id"])
                     for folder in folders if folder["path"] in chosen and not folder["known"]]
            if added:
                self.message_user(request, "Dodano foldery: " + ", ".join(folder.name for folder in added) + ".",
                                  messages.SUCCESS)
            else:
                self.message_user(request, "Nie zaznaczono nowych folderów.", messages.WARNING)
            return redirect("admin:people_dropboxfolder_changelist")
        return TemplateResponse(request, "admin/people/dropboxfolder/fetch.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta, "title": "Pobierz foldery z Dropboxa",
            "root": root, "folders": folders, "error": error_message,
            "missing": [] if error_message else missing_in_listing(folders, root),
        })

    def has_delete_permission(self, request, obj=None):
        # Zwykłe usuwanie wyłączone: folder usuwa się akcją „Usuń z listy”, która sprawdza stan w Dropboxie
        # i decyduje o dostępach osób (wiersze dostępu chroni PROTECT).
        return False


@admin.register(DropboxAccess)
class DropboxAccessAdmin(admin.ModelAdmin):
    """Podgląd stanu dostępów; poziom ustawia się na ekranie osoby."""
    list_display = ("person_label", "folder", "level", "wanted", "state", "granted_email", "short_error", "synced_at")
    list_filter = ("state", "level", "wanted", "folder")
    search_fields = ("person__first_name__plcontains", "person__last_name__plcontains", "granted_email")
    list_select_related = ("person", "folder")
    actions = ("sync_again",)
    readonly_fields = ("person", "folder", "level", "wanted", "state", "granted_email", "last_error", "synced_at",
                       "updated_at")

    @admin.display(description="osoba", ordering="person__last_name")
    def person_label(self, obj):
        return str(obj.person) if obj.person_id else f"osoba usunięta ({obj.granted_email or 'bez adresu'})"

    @admin.display(description="błąd")
    def short_error(self, obj):
        return obj.last_error[:120] or "–"

    @admin.action(description="Synchronizuj ponownie")
    def sync_again(self, request, queryset):
        sync_dropbox_rows(self, request, queryset)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Urlopy rejestrowane są w core.admin. Jedyny model rekrutacji to core.Recruitment.
