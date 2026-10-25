from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from core.permissions import (
    is_coordinator,
    require_team_member,
)
from people.models import Person, Vacation


def _require_access(user, person):
    require_team_member(user)

    if not person.is_active:
        raise ValidationError(
            "Nie można zmieniać urlopów osoby, która nie jest już w zespole."
        )

    if not is_coordinator(user) and person.user_id != user.pk:
        raise PermissionDenied(
            "Możesz zarządzać wyłącznie własnymi urlopami."
        )


def _lock_person(user, person_id):
    person = get_object_or_404(
        Person.objects.select_for_update(),
        pk=person_id,
    )
    _require_access(user, person)
    return person


def _lock_vacation(user, vacation_id):
    """
    Wywoływać wewnątrz transakcji.

    Najpierw ustalamy właściciela, następnie blokujemy osobę i urlop.
    Tę samą kolejność stosują wszystkie operacje tego modułu.
    """
    person_id = get_object_or_404(
        Vacation.objects.only("person_id"),
        pk=vacation_id,
    ).person_id

    person = _lock_person(user, person_id)
    vacation = get_object_or_404(
        Vacation.objects.select_for_update(),
        pk=vacation_id,
        person_id=person.pk,
    )
    vacation.person = person

    return vacation


def _validate_vacation_form(
    *,
    vacation,
    start_date,
    end_date,
    until_revoked,
):
    from core.forms import VacationForm

    if type(until_revoked) is not bool:
        raise ValidationError(
            "Nieprawidłowe oznaczenie urlopu do odwołania."
        )

    form = VacationForm(
        {
            "start_date": start_date,
            "end_date": end_date,
            "until_revoked": until_revoked,
        },
        instance=vacation,
    )

    if not form.is_valid():
        raise ValidationError(
            {
                field: list(errors)
                for field, errors in form.errors.items()
            }
        )

    vacation = form.save(commit=False)
    vacation.full_clean()
    return vacation


@transaction.atomic
def create_vacation(
    *,
    user,
    person_id,
    start_date,
    end_date=None,
    until_revoked=False,
):
    require_team_member(user)
    person = _lock_person(user, person_id)

    vacation = _validate_vacation_form(
        vacation=Vacation(person=person),
        start_date=start_date,
        end_date=end_date,
        until_revoked=until_revoked,
    )
    vacation.save()
    return vacation


@transaction.atomic
def update_vacation(
    *,
    user,
    vacation_id,
    start_date,
    end_date=None,
    until_revoked=False,
):
    require_team_member(user)
    vacation = _lock_vacation(user, vacation_id)
    now = timezone.now()

    if (
        not vacation.until_revoked
        and (
            vacation.end_date is None
            or vacation.end_date <= now
        )
    ):
        raise ValidationError("Zakończonego urlopu nie można edytować.")

    # Formularz otrzymuje świeżą instancję z bazy. Pozwala zachować
    # rozpoczęcie trwającego urlopu, ale nie cofnąć go jeszcze bardziej.
    vacation = _validate_vacation_form(
        vacation=vacation,
        start_date=start_date,
        end_date=end_date,
        until_revoked=until_revoked,
    )
    vacation.save(
        update_fields=[
            "start_date",
            "end_date",
            "until_revoked",
        ]
    )
    return vacation


@transaction.atomic
def finish_vacation(*, user, vacation_id):
    require_team_member(user)
    vacation = _lock_vacation(user, vacation_id)
    now = timezone.now()
    today = timezone.localdate(now)

    if not vacation.is_active:
        raise ValidationError("Można zakończyć wyłącznie trwający urlop.")

    if vacation.start_date > today:
        raise ValidationError(
            "Nie można zakończyć urlopu, który jeszcze się nie rozpoczął."
        )

    vacation.until_revoked = False
    vacation.end_date = now
    vacation.full_clean()
    vacation.save(update_fields=["until_revoked", "end_date"])
    return vacation


@transaction.atomic
def cancel_planned_vacation(*, user, vacation_id):
    vacation = _lock_vacation(user, vacation_id)
    if vacation.start_date <= timezone.localdate():
        raise ValidationError("Odwołać można wyłącznie urlop, który jeszcze się nie rozpoczął.")
    vacation.delete()
