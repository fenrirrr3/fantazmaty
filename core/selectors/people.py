from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from core.permissions import get_active_person_profile
from people.models import Vacation


UPCOMING_LEAVE_DAYS = 365


def user_leave_information(user):
    """
    Zwraca trwający urlop albo najbliższy zaplanowany urlop użytkownika.

    Użytkownik jest osobą, której dotyczy informacja. Uprawnienia
    odbiorcy sprawdza widok udostępniający te dane.

    Odczyt nie modyfikuje urlopów ani pól profilu osoby.
    """
    person = get_active_person_profile(user)

    if person is None:
        return None

    now = timezone.now()
    today = timezone.localdate(now)

    vacation = (
        Vacation.objects.filter(
            person_id=person.pk,
            person__is_active=True,
            start_date__lte=today + timedelta(days=UPCOMING_LEAVE_DAYS),
        )
        .filter(
            Q(until_revoked=True)
            | Q(end_date__gt=now)
        )
        .order_by("start_date", "pk")
        .first()
    )

    if vacation is None:
        return None

    # Trwające urlopy mają wcześniejszą datę rozpoczęcia niż przyszłe,
    # więc zaplanowanie kolejnego nie przesłania aktualnej nieobecności.
    is_active = vacation.start_date <= today
    is_upcoming = vacation.start_date > today

    # Jawna projekcja nie udostępnia szablonom relacji ORM prowadzących
    # z urlopu przez osobę i konto do innych danych aplikacji.
    vacation_data = {
        "pk": vacation.pk,
        "id": vacation.pk,
        "person_id": person.pk,
        "person": {
            "pk": person.pk,
            "first_name": person.first_name,
            "last_name": person.last_name,
        },
        "start_date": vacation.start_date,
        "end_date": vacation.end_date,
        "until_revoked": vacation.until_revoked,
        "created_at": vacation.created_at,
        "is_active": is_active,
        "is_upcoming": is_upcoming,
        "is_finished": False,
        "can_be_edited": True,
        "can_be_ended": is_active and vacation.until_revoked,
    }

    return {
        "vacation": vacation_data,
        "is_active": is_active,
        "is_upcoming": is_upcoming,
    }