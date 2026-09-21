"""Authenticate by email while retaining stable existing User identifiers."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        User = get_user_model()
        email = username.strip().lower()
        try:
            if not email:
                raise User.DoesNotExist
            user = User._default_manager.get(email__iexact=email)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            # Keep the password-hashing cost for unknown or ambiguous addresses.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
