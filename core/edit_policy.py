"""Per-view editing policy read by EditingMiddleware.

Widok sam deklaruje, czy jego formularz wymaga tokenu wersji i kto może
zapisać obiekt. Middleware nie przechowuje już list nazw adresów: reguła
leży przy widoku, a widok wywołuje tę samą funkcję sprawdzającą.

Funkcja ``check(request, obj, kwargs)`` zgłasza PermissionDenied. ``obj`` to
rekord nadrzędny wyznaczony przez ``core.middleware.aggregate`` (np. Text dla
notatki), a ``kwargs`` to argumenty adresu.
"""
from django.core.exceptions import PermissionDenied


class EditPolicy:
    def __init__(self, check=None, *, require_version=False):
        self.check = check
        self.require_version = require_version

    def allows(self, request, obj, kwargs):
        if self.check is None:
            return True
        try:
            self.check(request, obj, kwargs)
        except PermissionDenied:
            return False
        return True


def edit_policy(check=None, *, require_version=False):
    """Mark a view. Without permission the middleware skips the version check
    and lets the view respond with its own 403, so a conflict page never shows
    current values to someone who may not edit the object."""
    def decorator(view):
        view.edit_policy = EditPolicy(check, require_version=require_version)
        return view
    return decorator


def policy_for(match):
    return getattr(match.func, 'edit_policy', None) if match else None
