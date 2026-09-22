"""Only active superusers may use the Django admin."""
from types import MethodType
from django.contrib import admin
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

def has_permission(self, request):
    return request.user.is_active and request.user.is_superuser

def install():
    admin.site.has_permission = MethodType(has_permission, admin.site)
    admin.site.login_form = SuperuserAdminAuthenticationForm
    from core.admin_people import install as install_person_choices
    install_person_choices()
