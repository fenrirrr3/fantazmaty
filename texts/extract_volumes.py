from django.urls import reverse
from .models import ExtractVolume, ExtractVolumeCredit

PROFILE_TEXT_ROLES = ("Redakcja", "Korekta", "Korekta poskładowa", "Weryfikacja")

def volume_credit_groups(anthology):
    if not ExtractVolume.objects.filter(anthology=anthology).exists():
        return None
    groups = {}
    for credit in anthology.extract_credits.select_related("person"):
        groups.setdefault(credit.role, []).append(str(credit.person))
    return [{"label": role, "names": names} for role, names in groups.items()]


def profile_volume_credits(person):
    rows = []
    for credit in ExtractVolumeCredit.objects.filter(person=person, role__in=PROFILE_TEXT_ROLES).select_related("anthology"):
        book = credit.anthology
        rows.append(
            {
                "pk": credit.pk,
                "kind": "Tekst",
                "kind_key": "extract_volume",
                "detail_url": reverse("core:anthology_detail", args=[book.pk]),
                "role": credit.role,
                "get_role_display": credit.role,
                "assigned_at": None,
                "has_active_work": False,
                "has_reserved_work": False,
                "has_completed_work": True,
                "is_ready": True,
                "is_withdrawn": False,
                "text": {
                    "title": "Cała antologia",
                    "anthology": {"pk": book.pk, "title": book.title},
                    "authors": {"all": []},
                },
                "latest_stage": {
                    "started_at": None,
                    "ended_at": None,
                    "imported_completed": True,
                    "is_completed": True,
                },
            }
        )
    return rows
