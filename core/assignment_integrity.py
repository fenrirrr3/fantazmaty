"""Read-only checks of multiple assignments of one person to the same text."""
from collections import defaultdict

from django import forms
from django.db.models import Count, F, Q
from django.urls import reverse

from texts.models import Anthology
from workflow.models import WorkflowRoleAssignment


class AssignmentConflictTable:
    columns = {'Tekst': 'title', 'Osoba': 'person', 'Powód kontroli': 'reason', 'Przypisania, role i wykonania': 'assignments'}

    def __init__(self, queryset):
        self.queryset = queryset

    def sort_table(self, request):
        from core.sort_keys import sql_text_key
        query = self.queryset
        selected = request.GET.get('sort', '')
        fields = {'title': ('text__title', 'text__anthology__title'),
                  'person': ('assigned_to__first_name', 'assigned_to__last_name'),
                  'reason': ('role_count', 'assignment_count'),
                  'assignments': ('assignment_count', 'role_count')}.get(selected.lstrip('-'))
        if fields:
            ordering = []
            for index, field in enumerate(fields):
                key = field
                if '__' in field:
                    key = f'_sort_conflict_{index}'
                    query = query.annotate(**{key: sql_text_key(field, query.db)})
                ordering.append(F(key).desc(nulls_last=True) if selected.startswith('-') else F(key).asc(nulls_last=True))
            query = query.order_by(*ordering, 'text_id', 'assigned_to_id')
        return query, self.columns


class AssignmentIntegrityFilters(forms.Form):
    anthology = forms.ModelChoiceField(
        label='Antologia', queryset=Anthology.objects.all().order_by('title', 'pk'),
        required=False, empty_label='Wszystkie antologie',
    )
    scope = forms.ChoiceField(label='Zakres', required=False, initial='all', choices=(
        ('all', 'Wszystkie przebiegi i wykonania'),
        ('current_cycle', 'Bieżący przebieg, wszystkie wykonania'),
    ))
    kind = forms.ChoiceField(label='Rodzaj', required=False, initial='all', choices=(
        ('all', 'Wszystkie wielokrotne przypisania'),
        ('same_role', 'Ta sama rola w kilku przypisaniach'),
        ('different_roles', 'Różne role'),
    ))


def scoped_assignments(*, anthology_id=None, scope='all'):
    # Include previous executions and canceled repetitions, but label them in
    # the result. A current-only manager would hide the duplicates under review.
    query = WorkflowRoleAssignment.objects.all().filter(assigned_to__isnull=False)
    if anthology_id is not None:
        query = query.filter(text__anthology_id=anthology_id)
    if scope == 'current_cycle':
        query = query.filter(workflow_cycle=F('text__current_workflow_cycle'))
    return query


def assignment_conflicts(*, anthology_id=None, scope='all', kind='all'):
    # Count assignment records, not joined stages: several editorial passes
    # sharing one assignment do not mean the person was assigned twice.
    rows = scoped_assignments(anthology_id=anthology_id, scope=scope).order_by().values(
        'text_id', 'assigned_to_id', 'text__title', 'text__anthology__title',
        'assigned_to__last_name', 'assigned_to__first_name',
    ).annotate(assignment_count=Count('pk'), role_count=Count('role', distinct=True)).filter(
        assignment_count__gt=1,
    )
    if kind == 'same_role':
        rows = rows.filter(assignment_count__gt=F('role_count'))
    elif kind == 'different_roles':
        rows = rows.filter(role_count__gt=1)
    return rows.order_by('text__anthology__title', 'text__title', 'text_id',
                         'assigned_to__last_name', 'assigned_to__first_name', 'assigned_to_id')


def assignment_conflict_details(groups, *, anthology_id=None, scope='all'):
    """Hydrate just the requested page in one query, keeping each pair together."""
    groups = list(groups)
    if not groups:
        return []
    pairs = Q(pk__in=[])
    for row in groups:
        pairs |= Q(text_id=row['text_id'], assigned_to_id=row['assigned_to_id'])
    records = scoped_assignments(anthology_id=anthology_id, scope=scope).filter(pairs).select_related(
        'text', 'assigned_to__person_profile', 'repetition',
    ).order_by('workflow_cycle', 'role', 'execution_number', 'pk')
    grouped = defaultdict(list)
    for assignment in records:
        grouped[(assignment.text_id, assignment.assigned_to_id)].append(assignment)
    results = []
    for row in groups:
        assignments = grouped[(row['text_id'], row['assigned_to_id'])]
        if len(assignments) < 2:
            # A concurrent manual correction can resolve the pair after counting.
            continue
        roles = {assignment.role for assignment in assignments}
        user = assignments[0].assigned_to
        person = getattr(user, 'person_profile', None)
        name = user.get_full_name().strip() or (str(person) if person else '') or f'Konto #{user.pk}'
        details = []
        for assignment in assignments:
            canceled = assignment.repetition_id and assignment.repetition.canceled_at is not None
            if canceled:
                status = 'Anulowane powtórzenie'
            elif assignment.workflow_cycle != assignment.text.current_workflow_cycle:
                status = 'Poprzedni przebieg'
            elif not assignment.is_current:
                status = 'Historyczne wykonanie'
            else:
                status = 'Bieżące przypisanie'
            details.append({
                'id': assignment.pk, 'role': assignment.get_role_display(),
                'execution_number': assignment.execution_number,
                'workflow_cycle': assignment.workflow_cycle, 'status': status,
                'url': reverse('admin:workflow_workflowroleassignment_change', args=[assignment.pk]),
            })
        reasons = []
        if len(assignments) > len(roles):
            reasons.append('Ta sama rola w kilku przypisaniach')
        if len(roles) > 1:
            reasons.append('Różne role')
        results.append({
            'text_id': row['text_id'], 'title': row['text__title'],
            'anthology': row['text__anthology__title'] or 'Bez antologii',
            'text_url': reverse('core:assigned_text_detail', args=[row['text_id']]),
            'person_name': name, 'user_id': user.pk,
            'person_url': reverse('admin:auth_user_change', args=[user.pk]),
            'assignment_count': len(assignments), 'reasons': reasons, 'assignments': details,
        })
    return results
