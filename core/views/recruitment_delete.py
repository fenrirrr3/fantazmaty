from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from core.edit_versions import signed_state, signed_state_matches
from core.models import Recruitment
from core.permissions import superuser_required

SALT = 'recruitment-delete'


def deletion_snapshot(user, record):
    return [user.pk, record.pk, record.updated_at.isoformat(),
            [[pk, value.isoformat()] for pk, value in record.role_decisions.order_by('pk').values_list('pk', 'updated_at')]]


def deletion_token(user, record):
    return signed_state(deletion_snapshot(user, record), SALT)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def recruitment_delete(request, pk):
    with transaction.atomic():
        record = get_object_or_404(Recruitment.objects.select_for_update(), pk=pk)
        status, error = 200, ''
        if request.method == 'POST':
            if not signed_state_matches(request.POST.get('version', ''), deletion_snapshot(request.user, record), SALT):
                status, error = 409, 'Zgłoszenie zmieniło się lub potwierdzenie wygasło. Sprawdź dane przed usunięciem.'
            else:
                record.delete()
                messages.success(request, 'Usunięto zgłoszenie z bazy. Wiadomość na skrzynce pozostała bez zmian.')
                return redirect('core:recruitment_list')
        return render(request, 'core/recruitment_delete.html', {'record': record,
            'delete_version': deletion_token(request.user, record), 'error': error}, status=status)
