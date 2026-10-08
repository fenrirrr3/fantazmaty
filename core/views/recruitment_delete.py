from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from core.models import Recruitment
from core.permissions import superuser_required


def deletion_snapshot(user, record):
    return [user.pk, record.pk, record.updated_at.isoformat(),
            list(record.role_decisions.order_by('pk').values_list('pk', 'updated_at'))]


def deletion_token(user, record):
    snapshot = deletion_snapshot(user, record)
    snapshot[3] = [[pk, value.isoformat()] for pk, value in snapshot[3]]
    return signing.dumps(snapshot, salt='recruitment-delete')


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def recruitment_delete(request, pk):
    with transaction.atomic():
        record = get_object_or_404(Recruitment.objects.select_for_update(), pk=pk)
        status, error = 200, ''
        if request.method == 'POST':
            try:
                expected = signing.loads(request.POST.get('version', ''), salt='recruitment-delete', max_age=86400)
                current = signing.loads(deletion_token(request.user, record), salt='recruitment-delete')
            except (signing.BadSignature, TypeError):
                expected, current = None, True
            if expected != current:
                status, error = 409, 'Zgłoszenie zmieniło się lub potwierdzenie wygasło. Sprawdź dane przed usunięciem.'
            else:
                record.delete()
                messages.success(request, 'Usunięto zgłoszenie z bazy. Wiadomość na skrzynce pozostała bez zmian.')
                return redirect('core:recruitment_list')
        return render(request, 'core/recruitment_delete.html', {'record': record,
            'delete_version': deletion_token(request.user, record), 'error': error}, status=status)
