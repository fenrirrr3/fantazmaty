"""Shared role matching for dashboard counts and recruitment list filters."""
from collections import Counter

from core.models import Recruitment
from core.recruitment_roles import MANUAL_ROLE_CHOICES, ROLE_CHOICES


def role_choices():
    return [*ROLE_CHOICES, *MANUAL_ROLE_CHOICES]


def record_roles(mail_roles):
    """Rozpoznane role zgłoszenia; bez żadnej (np. mail spoza formularza) – „Inny pomysł”."""
    known = dict(role_choices())
    return {role for role in (mail_roles or []) if isinstance(role, str) and role in known} or {'other'}


def filter_role(records, role, *, status=''):
    # One related row must match both role and status.
    criteria = {'role_decisions__role': role}
    if status == 'mixed':
        criteria['status'] = status
    elif status:
        criteria['role_decisions__status'] = status
    ids = [pk for pk, roles in records.prefetch_related(None).values_list('pk', 'mail_roles').iterator()
           if role in record_roles(roles)]
    return records.filter(pk__in=ids, **criteria).distinct()


def pending_by_role():
    counts = Counter()
    records = Recruitment.objects.filter(status=Recruitment.Status.NEW).prefetch_related('role_decisions')
    for record in records:
        active_roles = record_roles(record.mail_roles)
        counts.update(row.role for row in record.role_decisions.all() if row.role in active_roles and row.status == 'new')
    return [{'role': key, 'label': label, 'count': counts[key]}
            for key, label in role_choices() if counts[key]]
