"""Read-only user preview; the authenticated session always belongs to its owner."""
from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from core.request_match import resolve_request
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST


SESSION_KEY = "_cms_user_preview"
CONTROL_VIEWS = {"core:user_preview", "core:user_preview_stop"}


def user_label(user):
    person = getattr(user, "person_profile", None)
    name = f"{person.first_name} {person.last_name}" if person else user.get_full_name()
    return f"{name or user.get_username()} – {user.email or user.get_username()}"


class PreviewUserChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return user_label(obj)


class UserPreviewForm(forms.Form):
    target = PreviewUserChoice(
        queryset=None, label="Użytkownik", empty_label="Wybierz użytkownika",
        widget=forms.Select(attrs={"data-searchable-select": "Szukaj po imieniu, nazwisku lub e-mailu"}),
    )

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].queryset = (
            get_user_model().objects.filter(is_active=True).exclude(pk=actor.pk)
            .select_related("person_profile")
            .order_by("person_profile__last_name", "person_profile__first_name", "email", "pk")
        )


def require_preview_owner(request):
    actor = getattr(request, "preview_actor", request.user)
    if not (actor.is_authenticated and actor.is_active and actor.is_superuser):
        raise PermissionDenied
    return actor


@never_cache
@require_http_methods(["GET", "POST"])
def user_preview(request):
    actor = require_preview_owner(request)
    form = UserPreviewForm(request.POST if request.method == "POST" else None, actor=actor)
    if request.method == "POST" and form.is_valid():
        target = form.cleaned_data["target"]
        request.session[SESSION_KEY] = {"actor": actor.pk, "target": target.pk}
        request._cms_activity = ("Włączenie podglądu użytkownika", f"user_id: #{target.pk}")
        return redirect("core:home")
    return render(request, "core/user_preview.html", {
        "form": form, "preview_enabled": SESSION_KEY in request.session,
    })


@never_cache
@require_POST
def user_preview_stop(request):
    require_preview_owner(request)
    request.session.pop(SESSION_KEY, None)
    return redirect("core:home")


class UserPreviewMiddleware:
    """Must run after activity middleware and before editing/permission checks."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.preview_actor = request.user
        request.is_user_preview = False
        state = request.session.get(SESSION_KEY)
        if state is None:
            return self.get_response(request)
        actor = request.preview_actor
        match = resolve_request(request)
        owner_valid = (
            actor.is_authenticated and actor.is_active and actor.is_superuser
            and isinstance(state, dict) and state.get("actor") == actor.pk
        )
        if not owner_valid:
            request.session.pop(SESSION_KEY, None)
            return self.blocked(request, "Podgląd został zakończony: brak uprawnień administratora.")
        if match and match.view_name in CONTROL_VIEWS:
            return self.uncached(self.get_response(request))
        target_id = state.get("target")
        target = None
        if type(target_id) is int and 0 < target_id < 2**63:
            target = get_user_model().objects.filter(pk=target_id, is_active=True).select_related("person_profile").first()
        if target is None or target.pk == actor.pk:
            request.session.pop(SESSION_KEY, None)
            return self.blocked(request, "Podgląd zakończony: wybrane konto nie jest już dostępne.")
        request.is_user_preview = True
        request.preview_label = user_label(target)
        request.user = request._cached_user = target

        async def preview_auser():
            return target

        request.auser = preview_auser
        request._acached_user = target
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            request._cms_activity = ("Zablokowana zmiana w podglądzie", f"user_id: #{target.pk}")
            return self.blocked(request, "Podgląd jest tylko do odczytu. Aby zapisać zmiany, zakończ podgląd.")
        if match and match.namespace not in ("core", "illustrations"):
            return self.blocked(request, "Aby przejść do administracji lub ustawień konta, zakończ podgląd.")
        response = self.get_response(request)
        if response.status_code == 403:
            return self.blocked(request, "Wybrany użytkownik nie ma dostępu do tej strony.")
        return self.uncached(response)

    @staticmethod
    def uncached(response):
        add_never_cache_headers(response)
        return response

    def blocked(self, request, reason):
        return self.uncached(render(request, "core/user_preview_blocked.html", {"reason": reason}, status=403))
