"""One claim policy shared by displayed availability and the mutation service."""
from core.permissions import is_coordinator, has_role
from workflow.models import WorkflowRoleAssignment as A


def eligible_role_users(role):
    """Candidates for a new assignment, using the same profile roles as writes."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from django.utils import timezone
    from people.models import Vacation
    from workflow.services import ROLE_GROUPS

    users = get_user_model().objects.filter(is_active=True).exclude(person_profile__is_external=True)
    required = ROLE_GROUPS.get(role)
    if not required:
        return users.none()
    coordinator = (Q(person_profile__roles__name__iexact='Koordynator')
                   | Q(person_profile__roles__name__istartswith='Koordynator ')
                   | Q(person_profile__legacy_coordinator_access=True))
    permitted = coordinator | Q(person_profile__roles__name__iexact=required)
    if role in (A.Role.PROOFREADER_2, A.Role.PROOFREADER_4):
        permitted = Q(person_profile__roles__name__iexact='Koordynator korekty')
    users = users.filter(Q(is_superuser=True) | (Q(person_profile__is_active=True) & permitted))
    if role == A.Role.STYLING:
        users = users.filter(is_superuser=True)
    now = timezone.now()
    leave = Vacation.objects.filter(
        person__user__isnull=False, start_date__lte=timezone.localdate(now),
    ).filter(Q(until_revoked=True) | Q(end_date__gt=now)).values('person__user_id')
    return users.exclude(pk__in=leave).distinct().order_by('last_name', 'first_name', 'pk')


def claim_access(user):
    from people.leave_access import is_on_leave
    if is_on_leave(user):
        return {"member": False, "coordinator": False, "roles": set()}
    from core.permissions import get_active_person_profile, _role_names
    if user.is_active and user.is_superuser:
        return {'member': True, 'coordinator': True, 'roles': set()}
    person = get_active_person_profile(user)
    if person is None:
        return {'member': False, 'coordinator': False, 'roles': set()}
    names = _role_names(person)
    return {'member': True, 'roles': names, 'coordinator': is_coordinator(user)}


def role_access_reason(user, role, *, access=None):
    """Policy for a new claim; assigned work may retain its former role."""
    from workflow.services import ROLE_GROUPS
    access = claim_access(user) if access is None else access
    if not access['member']:
        return 'Brak aktywnego dostępu do zespołu albo trwa urlop.'
    if role in (A.Role.PROOFREADER_2, A.Role.PROOFREADER_4) and not can_claim_fourth_proofreading(user):
        return 'Druga i czwarta korekta są dostępne tylko dla koordynatora korekty.'
    if role == A.Role.STYLING and not user.is_superuser:
        return 'Stylowanie jest dostępne tylko dla superusera.'
    if not (access['coordinator'] or ROLE_GROUPS.get(role, 'Redaktor').casefold() in access['roles']):
        return 'Brak wymaganej roli.'
    return ''


def claim_reason(stage, user, stages, assignments, *, access=None):
    if stage.text.anthology_id and stage.text.anthology.status == 'abandoned':
        return 'Antologia została porzucona.'
    from workflow.services import STAGE_ROLES
    access = claim_access(user) if access is None else access
    if stage.text.anthology_id and stage.text.anthology.status == "ready":
        return "Antologia jest gotowa."
    if not stage.is_current or not stage.is_released:
        return "Etap czeka na zakończenie poprzedniego."
    kind = stage.stage_type
    role = A.Role.EDITOR if kind == "ready_for_editing" else STAGE_ROLES.get(kind)
    if not access["member"]:
        return "Brak aktywnego dostępu do zespołu."
    if any(s.stage_type in ("ready", "withdrawn") for s in stages):
        return "Proces jest zamknięty."
    if stage.is_completed or stage.started_at or stage.ended_at:
        return "Etap nie oczekuje na przejęcie."
    if not role:
        return "Tego etapu nie można przejąć."
    reason = role_access_reason(user, role, access=access)
    if reason:
        return reason
    if (
        kind in ("second_proofreading", "third_proofreading")
        and first_proofreading_work(user).filter(text_id=stage.text_id).exists()
    ):
        return "Pierwszą korektę tego tekstu wykonywała już ta osoba."
    occupied = {a.role: a.assigned_to_id for a in assignments if a.assigned_to_id}
    if role in occupied:
        return "Rola ma już przypisanego wykonawcę."
    opposite = {"verifier_1": "verifier_2", "verifier_2": "verifier_1"}.get(role)
    if opposite and occupied.get(opposite) == user.pk:
        return "Pierwszą i drugą weryfikację wykonują różne osoby."
    if stage.repetition_id:
        return ""
    from workflow.services import completed_stage_exists

    if kind in ("first_verification", "second_verification") and completed_stage_exists(
        stage.text, kind
    ):
        return (
            stage.get_stage_type_display()
            + " jest już zakończona. Ponowne wykonanie wymaga powtórzenia etapów."
        )
    entry = len(stages) == 1 and not stage.is_completed
    if kind == "editor_control":
        return "Kontrolę rozpoczyna przypisany redaktor."
    if kind in ("editing", "author_editing") and not entry:
        return "Użyj przekazania lub wznowienia redakcji."
    if (
        kind == "first_verification"
        and not entry
        and not any(s.stage_type == "editing" and (s.started_at or s.is_completed) for s in stages)
    ):
        return "Rezerwacja wymaga rozpoczętej redakcji."
    if kind == "second_verification" and not entry:
        if not completed_stage_exists(stage.text, "first_verification"):
            return "Najpierw zakończ pierwszą weryfikację."
        if any(
            s.stage_type in ("editing", "author_editing") and not s.is_completed and not s.ended_at
            for s in stages
        ):
            return "Redaktor musi przekazać tekst do drugiej weryfikacji."
    return ""


def can_claim_fourth_proofreading(user):
    return bool(
        user and user.is_active and (user.is_superuser or has_role(user, "Koordynator korekty"))
    )


def first_proofreading_work(user):
    """All executions, including imports and former performers after handoffs."""
    from django.db.models import Q
    from django.utils import timezone

    return A.objects.filter(role=A.Role.PROOFREADER_1, assigned_to_id=user.pk).filter(
        Q(stages__is_completed=True)
        | Q(stages__started_at__lte=timezone.localdate())
        | Q(handoffs_from__isnull=False)
    )


def ensure_distinct_proofreader(text, role, user):
    from django.core.exceptions import ValidationError

    if (
        role in (A.Role.PROOFREADER_2, A.Role.PROOFREADER_3)
        and first_proofreading_work(user).filter(text_id=text.pk).exists()
    ):
        raise ValidationError(
            "Osoba wykonująca pierwszą korektę nie może przejąć drugiej ani trzeciej korekty tego tekstu."
        )
