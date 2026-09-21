from django.core.exceptions import ValidationError
from django.utils import timezone


def validate_archive_dates(assigned_at, opinion_at):
    errors = {}
    if assigned_at is not None:
        assigned = assigned_at if timezone.is_aware(assigned_at) else timezone.make_aware(assigned_at)
        if assigned > timezone.now():
            errors["assigned_at"] = "Data przydzielenia nie może być w przyszłości."
        if opinion_at and opinion_at < timezone.localdate(assigned):
            errors["opinion_changed_at"] = "Data opinii nie może być wcześniejsza niż przydzielenie."
    if opinion_at and opinion_at > timezone.localdate():
        errors["opinion_changed_at"] = "Data opinii nie może być w przyszłości."
    if errors:
        raise ValidationError(errors)
