"""Shared role matching for dashboard counts and recruitment list filters."""
from collections import Counter

from core.models import Recruitment, RecruitmentRoleDecision
from core.recruitment_roles import ROLE_CHOICES


def role_choices():
    choices = dict(ROLE_CHOICES)
    for key, label in Recruitment.Department.choices:
        choices.setdefault(key, label)
    return list(choices.items())


def record_roles(mail_roles, department):
    known = dict(role_choices())
    roles = {role for role in (mail_roles or []) if isinstance(role, str) and role in known}
    return roles or {department if department in known else 'other'}


def filter_role(records, role, *, status=''):
    # One related row must match both role and status.
    criteria = {'role_decisions__role': role}
    if status == 'mixed':
        criteria['status'] = status
    elif status:
        criteria['role_decisions__status'] = status
    ids = [pk for pk, roles, department in records.prefetch_related(None).values_list('pk', 'mail_roles', 'department').iterator()
           if role in record_roles(roles, department)]
    return records.filter(pk__in=ids, **criteria).distinct()


def pending_by_role():
    counts = Counter()
    records = Recruitment.objects.filter(status=Recruitment.Status.NEW).prefetch_related('role_decisions')
    for record in records:
        active_roles = record_roles(record.mail_roles, record.department)
        counts.update(row.role for row in record.role_decisions.all() if row.role in active_roles and row.status == 'new')
    return [{'role': key, 'label': label, 'count': counts[key]}
            for key, label in role_choices() if counts[key]]
