"""Shared role matching for dashboard counts and recruitment list filters."""
from collections import Counter

from core.models import Recruitment
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


def filter_role(records, role):
    # JSON contains is unsupported on SQLite. Use identical matching on both DBs.
    ids = [pk for pk, roles, department in records.values_list('pk', 'mail_roles', 'department').iterator()
           if role in record_roles(roles, department)]
    return records.filter(pk__in=ids)


def pending_by_role():
    counts = Counter()
    rows = Recruitment.objects.filter(status=Recruitment.Status.NEW).values_list('mail_roles', 'department')
    for roles, department in rows.iterator():
        counts.update(record_roles(roles, department))
    return [{'role': key, 'label': label, 'count': counts[key]}
            for key, label in role_choices() if counts[key]]
