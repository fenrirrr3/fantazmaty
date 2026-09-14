"""Read-only organizational dashboards. No repair actions are exposed."""
import csv
from collections import Counter
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET
from core.permissions import superuser_required, require_team_member, is_team_member, is_coordinator, is_reviewer, has_role
from core.supervision import integrity_issues, all_duplicates, anthology_checklist, anthology_credits
from core.selectors.texts import available_stages_for_user
from people.models import Person
from texts.models import Anthology
from workflow.services import ROLE_GROUPS
from workflow.models import WorkflowRoleAssignment

@login_required
@require_GET
@superuser_required
def data_integrity(request):
    tab = 'duplicates' if request.GET.get('tab') == 'duplicates' else 'integrity'
    rows = all_duplicates() if tab == 'duplicates' else integrity_issues()
    return render(request, 'core/data_integrity.html', {'issues': rows, 'tab': tab})

@login_required
@require_GET
def anthology_detail(request, anthology_id):
    require_team_member(request.user)
    anthology = get_object_or_404(Anthology, pk=anthology_id)
    coordinator = is_coordinator(request.user)
    return render(request, 'core/anthology_detail.html', {
        'anthology': anthology, 'show_checklist': coordinator,
        'issues': anthology_checklist(anthology) if coordinator else [],
        'credits': anthology_credits(anthology),
    })

def csv_value(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value

@login_required
@require_GET
def anthology_credits_csv(request, anthology_id):
    require_team_member(request.user)
    anthology = get_object_or_404(Anthology, pk=anthology_id)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="stopka-antologii-{anthology.pk}.csv"'
    response.write('\ufeff')
    writer = csv.writer(response, delimiter=';')
    writer.writerow(['Osoba', 'Praca', 'Teksty lub antologia'])
    for row in anthology_credits(anthology):
        writer.writerow([csv_value(row['name']), csv_value(row['role']), csv_value('; '.join(row['works']))])
    return response

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
    allowed = []
    for role, group in ROLE_GROUPS.items():
        permitted = active and (bool(user.is_superuser) if role == WorkflowRoleAssignment.Role.STYLING else coordinator or has_role(user, group))
        allowed.append({'label': dict(WorkflowRoleAssignment.Role.choices).get(role, role), 'allowed': permitted, 'source': 'Superuser' if role == WorkflowRoleAssignment.Role.STYLING else 'Koordynator / Superuser' if coordinator else group})
    available = available_stages_for_user(user=user) if active else []
    return render(request, 'core/person_permissions.html', {'person': person, 'roles': roles, 'groups': groups, 'permissions': allowed, 'available_count': len(available), 'access': active, 'coordinator': coordinator, 'reviewer': is_reviewer(user)})
