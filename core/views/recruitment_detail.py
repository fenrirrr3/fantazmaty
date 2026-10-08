from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.models import Recruitment
from core.permissions import coordinator_required
from core.recruitment_message import form_fields


class RecruitmentDecisionForm(forms.ModelForm):
    status = forms.ChoiceField(label='Decyzja', choices=(
        ('new', 'Bez decyzji'), ('accepted', 'Przyjęty'), ('rejected', 'Odrzucony')))

    class Meta:
        model = Recruitment
        fields = ('status', 'decision_reason')
        widgets = {'decision_reason': forms.Textarea(attrs={'rows': 6})}


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def recruitment_detail(request, pk):
    record = get_object_or_404(Recruitment, pk=pk)
    version = record.updated_at.isoformat()
    form = RecruitmentDecisionForm(request.POST if request.method == 'POST' else None, instance=record)
    status = 200
    if request.method == 'POST':
        version = request.POST.get('version', '')
        status = 400
        if form.is_valid():
            with transaction.atomic():
                current = get_object_or_404(Recruitment.objects.select_for_update(), pk=pk)
                if version != current.updated_at.isoformat():
                    form.add_error(None, 'Zgłoszenie zmieniło się w międzyczasie. Skopiuj uzasadnienie i odśwież dane przed ponownym zapisem.')
                    status = 409
                else:
                    current.status = form.cleaned_data['status']
                    current.decision_reason = form.cleaned_data['decision_reason']
                    current.save(update_fields=['status', 'decision_reason', 'updated_at'])
                    messages.success(request, 'Zapisano decyzję i uzasadnienie. Nie wysłano powiadomienia.')
                    return redirect('core:recruitment_detail', pk=pk)
        # ModelForm validation may mutate its instance; display persisted metadata.
        record.refresh_from_db()
    fields = form_fields(record.mail_body)
    return render(request, 'core/recruitment_detail.html', {
        'record': record, 'form': form, 'version': version,
        'candidate_name': record.full_name or fields.get('name') or record.mail_sender or 'Nie podano',
        'candidate_email': record.email or fields.get('email'),
    }, status=status)
