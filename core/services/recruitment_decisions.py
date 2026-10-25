"""Per-role decisions; callers lock the parent before writing child records."""
from django.utils import timezone
from core.models import Recruitment, RecruitmentRoleDecision
from core.selectors.recruitment import record_roles, role_choices


def ensure_decisions(record, *, using='default'):
    """Brakujące decyzje ról zgłoszenia – nowe zawsze „Bez decyzji” (dawne wspólne pola zgłoszenia zostają tylko
    do podglądu w adminie)."""
    roles = record_roles(record.mail_roles)
    existing = set(RecruitmentRoleDecision.objects.using(using).filter(recruitment_id=record.pk).values_list('role', flat=True))
    missing = [role for role, _ in role_choices() if role in roles and role not in existing]
    if missing:
        # One INSERT for all roles; the unique constraint keeps a concurrent writer harmless.
        RecruitmentRoleDecision.objects.using(using).bulk_create(
            [RecruitmentRoleDecision(recruitment_id=record.pk, role=role) for role in missing],
            ignore_conflicts=True,
        )


def refresh_summary(record, *, using='default'):
    roles = record_roles(record.mail_roles)
    statuses = list(RecruitmentRoleDecision.objects.using(using).filter(recruitment_id=record.pk, role__in=roles).values_list('status', flat=True))
    if len(statuses) < len(roles) or 'new' in statuses:
        value = 'new'
    elif len(set(statuses)) == 1:
        value = statuses[0]
    else:
        value = 'mixed'
    record.status = value
    Recruitment.objects.using(using).filter(pk=record.pk).update(status=value)
    record._prefetched_objects_cache = {}


def set_all_decisions(record, status):
    # The mailbox holds a transaction and the parent row lock.
    if status not in ('new', 'accepted', 'rejected'):
        raise ValueError('Nieprawidłowa decyzja')
    using = record._state.db or 'default'
    ensure_decisions(record, using=using)
    roles = record_roles(record.mail_roles)
    RecruitmentRoleDecision.objects.using(using).filter(recruitment_id=record.pk, role__in=roles).update(status=status, updated_at=timezone.now())
    refresh_summary(record, using=using)
