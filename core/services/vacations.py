from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from core.permissions import (
    is_superuser,
    require_team_member,
)
from people.models import Person, Vacation


def _require_access(user, person):
    require_team_member(user)

    if not person.is_active:
        raise ValidationError(
            "Nie można zmieniać urlopów osoby, która nie jest już w zespole."
        )

    if not is_superuser(user) and person.user_id != user.pk:
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

    return person, vacation


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


def _sync_person_leave(person, *, now):
    """
    Synchronizuje pola pomocnicze na podstawie wszystkich urlopów.

    Trwający urlop ma pierwszeństwo przed przyszłym. Gdy nie ma
    trwającego urlopu, wybieramy najbliższy zaplanowany.
    """
    today = timezone.localdate(now)
    vacations = list(
        Vacation.objects.select_for_update()
        .filter(person_id=person.pk)
        .order_by("start_date", "pk")
    )

    current = None
    upcoming = None

    for vacation in vacations:
        if (
            not vacation.until_revoked
            and (
                vacation.end_date is None
                or vacation.end_date <= now
            )
        ):
            continue

        if vacation.start_date <= today:
            if current is None:
                current = vacation
        elif upcoming is None:
            upcoming = vacation

    selected = current or upcoming
    values = {
        "leave_start_date": selected.start_date if selected else None,
        "leave_end_date": (
            selected.end_date
            if selected and not selected.until_revoked
            else None
        ),
        "leave_until_revoked": (
            selected.until_revoked if selected else False
        ),
    }
    changed_fields = []

    for field, value in values.items():
        if getattr(person, field) != value:
            setattr(person, field, value)
            changed_fields.append(field)

    if changed_fields:
        person.save(update_fields=changed_fields)


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

    _sync_person_leave(person, now=timezone.now())
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
    person, vacation = _lock_vacation(user, vacation_id)
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

    _sync_person_leave(person, now=timezone.now())
    return vacation


@transaction.atomic
def finish_vacation(*, user, vacation_id):
    require_team_member(user)
    person, vacation = _lock_vacation(user, vacation_id)
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

    _sync_person_leave(person, now=now)
    return vacation