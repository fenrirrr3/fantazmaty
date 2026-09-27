"""Organizational checks, publication tasks and team summaries."""
from core.sort_keys import text_key
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render, redirect
from django import forms
from django.db import transaction
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.views.decorators.http import require_GET, require_http_methods
from core.permissions import superuser_required, require_team_member, is_team_member, is_coordinator, is_reviewer, has_role
from core.supervision import integrity_issues, all_duplicates, anthology_checklist, anthology_credit_groups
from core.selectors.texts import available_stages_for_user
from people.models import Person, Role
from texts.models import Anthology, AnthologyTask
from workflow.services import ROLE_GROUPS
from workflow.models import WorkflowRoleAssignment

@login_required
@require_GET
@superuser_required
def data_integrity(request):
    tab = 'duplicates' if request.GET.get('tab') == 'duplicates' else 'integrity'
    selected = request.GET.get('anthology', 'all')
    scanned = False
    error = ''
    if tab == 'duplicates':
        rows = []
        if request.GET.get('run') == '1':
            anthology = Anthology.objects.filter(pk=int(selected)).first() if selected.isdecimal() and len(selected) < 19 else None
            if selected == 'all':
                rows = all_duplicates()
                scanned = True
            elif anthology is None:
                error = 'Wybierz antologię do sprawdzenia.'
            else:
                rows = all_duplicates(anthology.pk)
                scanned = True
    else:
        rows = integrity_issues()
    return render(request, 'core/data_integrity.html', {
        'issues': rows, 'tab': tab, 'scanned': scanned, 'scan_error': error,
        'anthologies': Anthology.objects.order_by('title') if tab == 'duplicates' else [],
        'selected_anthology': selected,
    })

class AnthologyTaskForm(forms.ModelForm):
    class Meta:
        model = AnthologyTask
        fields = ('status', 'assigned_to')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        selected = self.instance.assigned_to_id
        self.fields['assigned_to'].queryset = Person.objects.filter(
            Q(pk=selected) | Q(pk__in=Person.objects.active().values('pk'))
        ).order_by('last_name', 'first_name', 'pk')


def _task_forms(anthology, data=None):
    tasks = {task.task_type: task for task in anthology.production_tasks.all()}
    return [AnthologyTaskForm(data=data, prefix=kind,
                instance=tasks.get(kind) or AnthologyTask(anthology=anthology, task_type=kind))
            for kind in (AnthologyTask.TaskType.BANNERS, AnthologyTask.TaskType.BLURB, AnthologyTask.TaskType.TYPESETTING)]


@login_required
@require_http_methods(['GET', 'POST'])
def anthology_detail(request, anthology_id):
    require_team_member(request.user)
    anthology = get_object_or_404(Anthology, pk=anthology_id)
    coordinator = is_coordinator(request.user)
    forms_list = _task_forms(anthology) if coordinator else []
    if request.method == 'POST':
        if not coordinator:
            raise PermissionDenied
        with transaction.atomic():
            anthology = get_object_or_404(Anthology.objects.select_for_update(), pk=anthology_id)
            forms_list = _task_forms(anthology, request.POST)
            if all([form.is_valid() for form in forms_list]):
                for form in forms_list:
                    form.save()
                messages.success(request, 'Zapisano zadania antologii.')
                return redirect('core:anthology_detail', anthology_id=anthology.pk)
    return render(request, 'core/anthology_detail.html', {
        'anthology': anthology, 'show_checklist': coordinator, 'task_forms': forms_list,
        'issues': anthology_checklist(anthology) if coordinator else [],
        'credits': anthology_credit_groups(anthology),
    }, status=400 if request.method == 'POST' else 200)


@login_required
@require_GET
@superuser_required
def role_names(request):
    roles = Role.objects.order_by('name')
    selected = request.GET.get('role', '')
    role = roles.filter(pk=int(selected)).first() if selected.isdecimal() and len(selected) < 19 else None
    names = []
    if role is not None:
        people = Person.objects.active().filter(
            Q(roles=role) | Q(user__groups__name__iexact=role.name)
        ).distinct()
        names = sorted((f'{person.first_name} {person.last_name}'.strip() for person in people), key=text_key)
    return render(request, 'core/role_names.html', {'roles': roles, 'selected_role': selected, 'names': names, 'chosen_role': role})


@login_required
@require_GET
@superuser_required
def person_permissions(request, person_id):
    person = get_object_or_404(Person.objects.select_related('user', 'author_profile'), pk=person_id)
    user = person.user
    roles = list(person.roles.values_list('name', flat=True))
    groups = list(user.groups.values_list('name', flat=True)) if user else []
    active = is_team_member(user)
    coordinator = is_coordinator(user)
    from people.leave_access import is_on_leave
    on_leave = is_on_leave(user) if user else False
    allowed = []
    for role, group in ROLE_GROUPS.items():
        permitted = active and (bool(user.is_superuser) if role == WorkflowRoleAssignment.Role.STYLING else coordinator or has_role(user, group))
        allowed.append({'label': dict(WorkflowRoleAssignment.Role.choices).get(role, role), 'allowed': permitted and not on_leave, 'role_allowed': permitted, 'block': 'Urlop' if permitted and on_leave else '', 'source': 'Superuser' if role == WorkflowRoleAssignment.Role.STYLING else 'Koordynator / Superuser' if coordinator else group})
    available = available_stages_for_user(user=user) if active else []
    return render(request, 'core/person_permissions.html', {'person': person, 'roles': roles, 'groups': groups, 'permissions': allowed, 'available_count': len(available), 'access': active, 'coordinator': coordinator, 'reviewer': is_reviewer(user)})
