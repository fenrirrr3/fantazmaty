"""Only active superusers may use the Django admin."""
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
