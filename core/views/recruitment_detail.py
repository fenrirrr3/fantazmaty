from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from core.models import Recruitment, RecruitmentRoleDecision
from core.permissions import coordinator_required
from core.recruitment_message import form_fields
from core.selectors.recruitment import record_roles, role_choices


class RecruitmentDecisionForm(forms.ModelForm):
    class Meta:
        model = RecruitmentRoleDecision
        fields = ('status', 'decision_reason', 'unofficial_notes')
        labels = {'status': 'Decyzja', 'decision_reason': 'Uzasadnienie decyzji', 'unofficial_notes': 'Nieoficjalne notatki'}
        widgets = {name: forms.Textarea(attrs={'rows': 4}) for name in ('decision_reason', 'unofficial_notes')}


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def recruitment_detail(request, pk):
    record = get_object_or_404(Recruitment, pk=pk)
    roles = record_roles(record.mail_roles, record.department)
    selected_role = request.POST.get('role', '') if request.method == 'POST' else ''
    decisions = {row.role: row for row in record.role_decisions.all() if row.role in roles}
    if request.method == 'POST' and selected_role not in decisions:
        return HttpResponseBadRequest('Nieprawidłowa rola zgłoszenia. Odśwież dane.')
    sections = []
    status = 200
    for role, label in role_choices():
        if role not in decisions:
            continue
        decision = decisions[role]
        selected = role == selected_role
        form = RecruitmentDecisionForm(request.POST if selected else None, instance=decision, prefix=role)
        version = request.POST.get('version', '') if selected else decision.updated_at.isoformat()
        if selected:
            status = 400
            if form.is_valid():
                with transaction.atomic():
                    parent = get_object_or_404(Recruitment.objects.select_for_update(), pk=pk)
                    current = get_object_or_404(RecruitmentRoleDecision.objects.select_for_update(), recruitment_id=pk, role=role)
                    if role not in record_roles(parent.mail_roles, parent.department) or version != current.updated_at.isoformat():
                        form.add_error(None, 'Decyzja dla tej roli zmieniła się w międzyczasie. Skopiuj wpisane dane i odśwież formularz.')
                        status = 409
                    else:
                        for field in form.Meta.fields:
                            setattr(current, field, form.cleaned_data[field])
                        current.save(update_fields=[*form.Meta.fields, 'updated_at'])
                        parent.save(update_fields=['updated_at'])
                        messages.success(request, f'Zapisano decyzję dla roli: {label}. Nie wysłano powiadomienia.')
                        return redirect('core:recruitment_detail', pk=pk)
        sections.append({'role': role, 'label': label, 'form': form, 'version': version, 'decision': decision, 'open': selected})
    fields = form_fields(record.mail_body)
    from .recruitment_delete import deletion_token
    return render(request, 'core/recruitment_detail.html', {
        'record': record, 'sections': sections,
        'candidate_name': record.full_name or fields.get('name') or record.mail_sender or 'Nie podano',
        'candidate_email': record.email or fields.get('email'),
        'delete_version': deletion_token(request.user, record) if request.user.is_superuser else '',
    }, status=status)
