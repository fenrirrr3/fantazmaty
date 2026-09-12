from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from core.forms import VacationForm
from core.pagination import paginate_items
from core.permissions import (
    get_active_person_profile,
    is_superuser,
    team_member_required,
)
from core.services.vacations import (
    create_vacation,
    finish_vacation,
    update_vacation,
)
from people.models import Vacation


# Serwisy zapisujące urlopy sprawdzają uprawnienia ponownie.
# W jednej transakcji blokują najpierw Person, następnie Vacation,
# walidują aktualny stan i synchronizują pola urlopowe osoby.
# Synchronizacja uwzględnia wszystkie jej urlopy: przyszły urlop
# nie może przesłonić urlopu trwającego obecnie.


def _manageable_vacations(user):
    queryset = Vacation.objects.filter(
        person__is_active=True,
    ).select_related("person")

    if is_superuser(user):
        return queryset

    person = get_active_person_profile(user)

    if person is None:
        return queryset.none()

    return queryset.filter(person_id=person.pk)


def _vacation_redirect(user, vacation):
    if vacation.person.user_id == user.pk:
        return redirect("core:my_vacations")

    return redirect("core:active_vacations")


def _add_validation_errors(form, error):
    if hasattr(error, "error_dict"):
        for field, errors in error.error_dict.items():
            target = field if field in form.fields else None

            for item in errors:
                form.add_error(target, item)
    else:
        form.add_error(None, error)


def _render_my_vacations(request, person, form, *, status=200):
    vacations = (
        Vacation.objects.filter(person_id=person.pk)
        .select_related("person")
        .order_by("-start_date", "-created_at", "-pk")
    )
    page_obj = paginate_items(request, vacations)

    return render(
        request,
        "core/my_vacations.html",
        {
            "form": form,
            "person": person,
            "vacations": page_obj,
            "page_obj": page_obj,
        },
        status=status,
    )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def my_vacations(request):
    person = get_active_person_profile(request.user)

    if person is None:
        messages.error(
            request,
            "Twoje konto nie jest połączone z aktywnym członkiem zespołu.",
        )
        return redirect("core:home")

    form = VacationForm(
        request.POST if request.method == "POST" else None,
        instance=Vacation(person=person),
    )

    if request.method == "POST" and form.is_valid():
        try:
            create_vacation(
                user=request.user,
                person_id=person.pk,
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
                until_revoked=form.cleaned_data["until_revoked"],
            )
        except ValidationError as error:
            _add_validation_errors(form, error)
        else:
            messages.success(request, "Zgłoszono urlop.")
            return redirect("core:my_vacations")

    return _render_my_vacations(
        request,
        person,
        form,
        status=400 if request.method == "POST" else 200,
    )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def edit_vacation(request, vacation_id):
    vacation = get_object_or_404(
        _manageable_vacations(request.user),
        pk=vacation_id,
    )

    if not vacation.can_be_edited:
        messages.error(
            request,
            "Zakończonego urlopu nie można edytować.",
        )
        return _vacation_redirect(request.user, vacation)

    form = VacationForm(
        request.POST if request.method == "POST" else None,
        instance=vacation,
    )

    if request.method == "POST" and form.is_valid():
        try:
            # Serwis pobiera świeżą, zablokowaną instancję.
            # Nie zapisuje instancji zmodyfikowanej przez ModelForm.
            update_vacation(
                user=request.user,
                vacation_id=vacation.pk,
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
                until_revoked=form.cleaned_data["until_revoked"],
            )
        except ValidationError as error:
            _add_validation_errors(form, error)
        else:
            messages.success(request, "Zapisano zmiany urlopu.")
            return _vacation_redirect(request.user, vacation)

    if request.method == "POST":
        # Nagłówek opisuje zapisany urlop, a formularz zachowuje
        # przesłane wartości oraz błędy walidacji.
        vacation = get_object_or_404(
            _manageable_vacations(request.user),
            pk=vacation_id,
        )

    return render(
        request,
        "core/edit_vacation.html",
        {
            "form": form,
            "vacation": vacation,
            "person": vacation.person,
        },
        status=400 if request.method == "POST" else 200,
    )


@never_cache
@login_required
@require_POST
@team_member_required
def end_vacation(request, vacation_id):
    vacation = get_object_or_404(
        _manageable_vacations(request.user),
        pk=vacation_id,
    )

    try:
        # Serwis kończy wyłącznie rozpoczęty urlop do odwołania.
        # Datę i godzinę zakończenia ustala po stronie serwera.
        finish_vacation(
            user=request.user,
            vacation_id=vacation.pk,
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Zakończono urlop.")

    return _vacation_redirect(request.user, vacation)


@never_cache
@login_required
@require_GET
@team_member_required
def active_vacations(request):
    now = timezone.now()
    today = timezone.localdate(now)

    vacations = (
        Vacation.objects.filter(
            person__is_active=True,
            start_date__lte=today,
        )
        .filter(
            Q(until_revoked=True)
            | Q(end_date__gt=now)
        )
        .select_related("person")
        .prefetch_related("person__roles")
        .order_by(
            "person__last_name",
            "person__first_name",
            "person_id",
            "-start_date",
            "-created_at",
            "-pk",
        )
    )
    page_obj = paginate_items(request, vacations)

    return render(
        request,
        "core/active_vacations.html",
        {
            "vacations": page_obj,
            "page_obj": page_obj,
            "today": today,
            "now": now,
        },
    )