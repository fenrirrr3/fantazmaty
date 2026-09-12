from django import forms
from django.contrib import admin
from django.db.models import Value
from django.db.models.functions import Lower

from .models import Person, Role


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
            "leave_start_date",
            "leave_end_date",
            "leave_until_revoked",
        )

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def clean_email(self):
        email = self.cleaned_data["email"].strip()

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
        "user",
    )

    list_per_page = 50
    show_full_result_count = False

    fieldsets = (
        (
            "Dane osoby",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                    "dropbox_email",
                    "previous_data",
                ),
            },
        ),
        (
            "Przynależność do zespołu",
            {
                "fields": (
                    "is_active",
                    "roles",
                    "is_coordinator",
                    "user",
                ),
                "description": (
                    "Odznaczenie „wciąż w ekipie” ukrywa osobę "
                    "na liście aktywnego zespołu. Zachowuje jej "
                    "profil i historyczne powiązania."
                ),
            },
        ),
        (
            "Urlop",
            {
                "fields": (
                    "leave_start_date",
                    "leave_end_date",
                    "leave_until_revoked",
                ),
            },
        ),
    )

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
