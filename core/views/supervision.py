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
from core.supervision import unlinked_review_candidates, integrity_issues, anthology_checklist, anthology_credit_groups
from core.assignment_integrity import AssignmentIntegrityFilters, assignment_conflicts, assignment_conflict_details
from core.pagination import paginate_items
from core.selectors.texts import available_stages_for_user
from people.models import Person, Role
from texts.models import Anthology, AnthologyTask
from workflow.services import ROLE_GROUPS
from workflow.models import WorkflowRoleAssignment

@login_required
@require_GET
@superuser_required
def data_integrity(request):
    requested_tab = request.GET.get('tab')
    # Old bookmarks open the replacement report without running title matching.
    if requested_tab == 'duplicates':
        requested_tab = 'assignments'
    tab = requested_tab if requested_tab in ('assignments', 'unlinked') else 'integrity'
    candidate_page = None
    assignment_page = None
    assignment_rows = []
    assignment_filters = None
    if tab == 'unlinked':
        from django.core.paginator import Paginator
        candidate_page = Paginator(unlinked_review_candidates(), 50).get_page(request.GET.get('page'))
        rows = []
    elif tab == 'assignments':
        rows = []
        parameters = request.GET.copy()
        if parameters.get('anthology') == 'all':
            parameters['anthology'] = ''
        parameters.setdefault('scope', 'all')
        parameters.setdefault('kind', 'all')
        assignment_filters = AssignmentIntegrityFilters(parameters)
        if assignment_filters.is_valid():
            data = assignment_filters.cleaned_data
            scope = {'anthology_id': data['anthology'].pk if data['anthology'] else None,
                     'scope': data['scope'] or 'all'}
            assignment_page = paginate_items(request, assignment_conflicts(
                **scope, kind=data['kind'] or 'all'), default=25)
            assignment_rows = assignment_conflict_details(assignment_page.object_list, **scope)
    else:
        rows = integrity_issues()
    return render(request, 'core/data_integrity.html', {
        'issues': rows, 'tab': tab, 'candidate_page': candidate_page,
        'assignment_filters': assignment_filters, 'assignment_page': assignment_page,
        'assignment_rows': assignment_rows,
    })

class AnthologyTaskForm(forms.ModelForm):
    class Meta:
        model = AnthologyTask
        fields = ('status', 'assigned_to')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].widget.attrs['data-searchable-person'] = 'true'
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
    from core.selectors.people import role_names_context
    return render(request, 'core/role_names.html', role_names_context(request.GET))


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
    from workflow.availability import claim_access, role_access_reason
    access = claim_access(user) if user else None
    allowed = []
    for role, group in ROLE_GROUPS.items():
        reason = role_access_reason(user, role, access=access) if user else 'Brak powiązanego konta.'
        allowed.append({'label': dict(WorkflowRoleAssignment.Role.choices).get(role, role),
                        'allowed': not reason, 'role_allowed': not reason, 'block': reason,
                        'source': 'Superuser' if user and user.is_superuser else group})
    available = available_stages_for_user(user=user) if active else []
    return render(request, 'core/person_permissions.html', {'person': person, 'roles': roles, 'groups': groups, 'permissions': allowed, 'available_count': len(available), 'access': active, 'coordinator': coordinator, 'reviewer': is_reviewer(user)})
