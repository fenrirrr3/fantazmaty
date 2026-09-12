from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Value
from django.db.models.functions import Lower
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from core.normalization import AUTHOR_FIELDS, NormalizedFormMixin

from .models import Author, AuthorNote, BlacklistedAuthor


class SuperuserOnlyAdminMixin:
    """Dostęp do danych autorów w adminie wyłącznie dla superusera."""

    @staticmethod
    def has_superuser_access(request):
        user = request.user
        return (
            user.is_authenticated
            and user.is_active
            and user.is_staff
            and user.is_superuser
        )

    def has_module_permission(self, request):
        return self.has_superuser_access(request)

    def has_view_permission(self, request, obj=None):
        return self.has_superuser_access(request)

    def has_add_permission(self, request, obj=None):
        return self.has_superuser_access(request)

    def has_change_permission(self, request, obj=None):
        return self.has_superuser_access(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_superuser_access(request)

    def history_view(self, request, object_id, extra_context=None):
        if not self.has_superuser_access(request):
            raise PermissionDenied(
                "Historia danych autorów jest dostępna wyłącznie dla superusera."
            )
        return super().history_view(request, object_id, extra_context=extra_context)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)

        if not self.has_superuser_access(request):
            return queryset.none()

        return queryset


class AuthorAdminForm(NormalizedFormMixin, forms.ModelForm):
    normalization_fields = AUTHOR_FIELDS
    confirm_possible_duplicate = forms.BooleanField(
        label="Sprawdziłem podobnych autorów i potwierdzam zapis",
        required=False,
        help_text=(
            "Potwierdzenie jest wymagane tylko przy wykryciu podobnego "
            "autora. Nie pozwala zapisać powtórzonego adresu e-mail."
        ),
        widget=forms.CheckboxInput(
            attrs={
                "class": "possible-duplicate-confirmation",
            }
        ),
    )

    class Meta:
        model = Author
        fields = (
            "first_name",
            "last_name",
            "pseudonym",
            "email",
            "has_contract",
            "contact",
            "is_blacklisted",
        )

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def clean_pseudonym(self):
        return " ".join(self.cleaned_data["pseudonym"].split())

    def clean_email(self):
        email = self.cleaned_data["email"].strip()

        # LOWER po obu stronach zapewnia porównanie bez względu na wielkość
        # liter również przy kolacji MySQL rozróżniającej wielkość liter.
        queryset = Author.objects.alias(
            email_lower=Lower("email"),
        ).filter(email_lower=Lower(Value(email)))

        if self.instance.pk is not None:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise forms.ValidationError(
                "Autor z tym adresem e-mail już istnieje. "
                "Wybierz istniejący rekord zamiast tworzyć nowy.",
                code="duplicate_email",
            )

        return email

    def clean(self):
        cleaned_data = super().clean()

        if self.errors:
            return cleaned_data

        identity_fields = {
            "first_name",
            "last_name",
            "pseudonym",
            "email",
        }

        # Zmiana samej flagi czarnej listy, umowy lub kontaktu
        # nie wymaga ponownego potwierdzania podobieństwa nazwisk.
        if (
            not self.instance._state.adding
            and not identity_fields.intersection(self.changed_data)
        ):
            return cleaned_data

        first_name = cleaned_data.get("first_name", "")
        last_name = cleaned_data.get("last_name", "")
        pseudonym = cleaned_data.get("pseudonym", "")

        conditions = Q()

        if first_name and last_name:
            conditions |= Q(
                first_name_lower=Lower(Value(first_name)),
                last_name_lower=Lower(Value(last_name)),
            )

        if pseudonym:
            conditions |= Q(pseudonym_lower=Lower(Value(pseudonym)))

        if not conditions:
            return cleaned_data

        queryset = Author.objects.alias(
            first_name_lower=Lower("first_name"),
            last_name_lower=Lower("last_name"),
            pseudonym_lower=Lower("pseudonym"),
        ).filter(conditions)

        if self.instance.pk is not None:
            queryset = queryset.exclude(pk=self.instance.pk)

        duplicates = list(
            queryset.order_by(
                "last_name",
                "first_name",
                "pk",
            ).values(
                "pk",
                "first_name",
                "last_name",
                "pseudonym",
                "email",
            )[:11]
        )

        if not duplicates or cleaned_data.get("confirm_possible_duplicate"):
            return cleaned_data

        descriptions = []

        for author in duplicates[:10]:
            description = (
                f"#{author['pk']}: "
                f"{author['first_name']} {author['last_name']}"
            )

            if author["pseudonym"]:
                description += f" ({author['pseudonym']})"

            description += f" – {author['email']}"
            descriptions.append(description)

        message = (
            "Wykryto podobnych autorów: "
            + "; ".join(descriptions)
            + "."
        )

        if len(duplicates) > 10:
            message += " Istnieją również dalsze pasujące rekordy."

        message += (
            " Sprawdź istniejące dane. Jeśli jest to inna osoba, "
            "zaznacz potwierdzenie i zapisz formularz ponownie."
        )

        self.add_error(
            "confirm_possible_duplicate",
            forms.ValidationError(
                message,
                code="possible_duplicate_author",
            ),
        )

        return cleaned_data


class AuthorNoteInline(SuperuserOnlyAdminMixin, admin.StackedInline):
    model = AuthorNote
    extra = 0

    fields = (
        "content",
        "created_by",
        "created_at",
    )

    readonly_fields = (
        "created_by",
        "created_at",
    )

    ordering = (
        "-created_at",
        "-pk",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "author",
            "created_by",
        )


@admin.register(Author)
class AuthorAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = AuthorAdminForm

    list_display = (
        "full_name",
        "pseudonym",
        "email",
        "has_contract",
        "contact",
        "blacklist_status",
    )

    list_display_links = (
        "full_name",
    )

    list_filter = (
        "is_blacklisted",
        "has_contract",
        "contact",
    )

    search_fields = (
        "first_name__plcontains",
        "last_name__plcontains",
        "pseudonym__plcontains",
        "email__plcontains",
        "notes__content__plcontains",
    )

    ordering = (
        "last_name",
        "first_name",
        "pk",
    )

    list_per_page = 50
    show_full_result_count = False

    fieldsets = (
        (
            "Dane autora",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "pseudonym",
                    "email",
                ),
            },
        ),
        (
            "Współpraca",
            {
                "fields": (
                    "has_contract",
                    "contact",
                ),
            },
        ),
        (
            "Czarna lista",
            {
                "fields": (
                    "is_blacklisted",
                ),
                "description": (
                    "Oznaczenie jest dostępne wyłącznie "
                    "w panelu administracyjnym. Powoduje ostrzeżenie "
                    "przed dodaniem zgłoszenia autora do recenzji."
                ),
            },
        ),
        (
            "Kontrola duplikatów",
            {
                "fields": (
                    "confirm_possible_duplicate",
                ),
            },
        ),
    )

    inlines = (
        AuthorNoteInline,
    )

    def get_urls(self):
        return [
            path(
                "blacklist/",
                self.admin_site.admin_view(self.blacklist_view),
                name="authors_author_blacklist",
            ),
        ] + super().get_urls()

    def blacklist_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied

        changelist_url = reverse(
            f"{self.admin_site.name}:authors_author_changelist"
        )

        return HttpResponseRedirect(
            f"{changelist_url}?is_blacklisted__exact=1"
        )

    @admin.display(
        description="nazwisko i imię",
        ordering="last_name",
    )
    def full_name(self, obj):
        return f"{obj.last_name} {obj.first_name}".strip()

    @admin.display(description="czarna lista", ordering="is_blacklisted")
    def blacklist_status(self, obj):
        return format_html(
            '<strong style="display:inline-block;padding:4px 9px;border-radius:4px;'
            'background:{};color:{}" aria-label="Czarna lista: {}">{}</strong>',
            "#fce4e4" if obj.is_blacklisted else "#edf2f6",
            "#991b1b" if obj.is_blacklisted else "#465467",
            "tak" if obj.is_blacklisted else "nie",
            "✓ TAK" if obj.is_blacklisted else "✗ NIE",
        )

    def save_formset(self, request, form, formset, change):
        if not self.has_change_permission(request, form.instance):
            raise PermissionDenied

        instances = formset.save(commit=False)

        for deleted_instance in formset.deleted_objects:
            deleted_instance.delete()

        for instance in instances:
            if isinstance(instance, AuthorNote) and instance._state.adding:
                instance.created_by = request.user

            instance.save()

        formset.save_m2m()


@admin.register(BlacklistedAuthor)
class BlacklistedAuthorAdmin(AuthorAdmin):
    list_filter = ("has_contract", "contact")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_blacklisted=True)

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        # Nie rejestruj drugi raz adresu authors_author_blacklist.
        return admin.ModelAdmin.get_urls(self)


@admin.register(AuthorNote)
class AuthorNoteAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "author",
        "created_by",
        "created_at",
        "content_preview",
    )

    list_filter = (
        "created_at",
    )

    search_fields = (
        "author__first_name__plcontains",
        "author__last_name__plcontains",
        "author__pseudonym__plcontains",
        "author__email__plcontains",
        "content__plcontains",
    )

    autocomplete_fields = (
        "author",
    )

    fields = (
        "author",
        "content",
        "created_by",
        "created_at",
    )

    readonly_fields = (
        "created_by",
        "created_at",
    )

    list_select_related = (
        "author",
        "created_by",
    )

    ordering = (
        "-created_at",
        "-pk",
    )

    list_per_page = 50
    show_full_result_count = False

    def save_model(self, request, obj, form, change):
        if not self.has_superuser_access(request):
            raise PermissionDenied

        if obj._state.adding:
            obj.created_by = request.user

        super().save_model(request, obj, form, change)

    @admin.display(description="treść")
    def content_preview(self, obj):
        content = " ".join(obj.content.split())

        if len(content) <= 100:
            return content

        return f"{content[:97]}..."
