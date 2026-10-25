from django.core.exceptions import ValidationError
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
    return Vacation.objects.filter(person=person).active().exists()


def require_available(user, *, lock=True):
    if is_on_leave(user, lock=lock):
        raise ValidationError("W trakcie urlopu nie można przejmować, rezerwować ani rozpoczynać nowych prac.")
