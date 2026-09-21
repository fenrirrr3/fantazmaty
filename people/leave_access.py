from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from people.models import Person, Vacation


def is_on_leave(user, *, lock=False):
    if not user or not user.pk:
        return False
    people = Person.objects.filter(user_id=user.pk)
    if lock:
        people = people.select_for_update()
    person = people.first()
    if not person:
        return False
    now = timezone.now()
    return Vacation.objects.filter(person=person, start_date__lte=timezone.localdate(now)).filter(
        Q(until_revoked=True) | Q(end_date__gt=now)
    ).exists()


def require_available(user):
    if is_on_leave(user, lock=True):
        raise ValidationError("W trakcie urlopu nie można przejmować, rezerwować ani rozpoczynać nowych prac.")
