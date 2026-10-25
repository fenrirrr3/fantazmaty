"""Panel administracyjny: logowanie tylko aktywnego superusera i wspólne domieszki uprawnień adminów."""
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

class SuperuserAdminAuthenticationForm(AdminAuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.auth_forms import configure_email_login
        configure_email_login(self)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_superuser:
            raise ValidationError("Panel administracyjny jest dostępny tylko dla superusera.", code="invalid_login")


class ReadOnlyAdminMixin:
    """Admin tylko do podglądu: rekordy tworzy CMS (dzienniki, migawki), w panelu bez dodawania, zmian i usuwania."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SingleRecordAdminMixin:
    """Ustawienia w jednym rekordzie: dodanie tylko, gdy rekordu jeszcze nie ma; bez usuwania."""

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not self.model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
