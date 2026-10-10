"""Save the notification flag independently of role decisions and candidate data."""
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from core.models import Recruitment
from core.permissions import coordinator_required, require_recruitment_mailbox

SALT = 'recruitment-notified'


class NotificationConflict(Exception):
    pass


def notified_snapshot(user, record):
    return [user.pk, record.pk, record.notified, record.notified_at.isoformat() if record.notified_at else '']


def notified_token(user, record):
    return signing.dumps(notified_snapshot(user, record), salt=SALT)


def save_notification(record, user, value, token, *, new_mail=False):
    # The caller holds the parent row lock and transaction.
    require_recruitment_mailbox(user)
    if new_mail and not token:
        valid = not record.notified and record.notified_at is None
    else:
        try:
            valid = signing.loads(token, salt=SALT, max_age=86400) == notified_snapshot(user, record)
        except (signing.BadSignature, TypeError):
            valid = False
    if not valid:
        raise NotificationConflict('Status powiadomienia zmienił się lub formularz wygasł. Odśwież listę.')
    record.notified = value
    record.save(update_fields=['notified', 'updated_at'])


def notification_response(record, user):
    return {'notified': record.notified, 'version': notified_token(user, record),
            'url': reverse('core:recruitment_notified', args=[record.pk]),
            'record_url': reverse('core:recruitment_detail', args=[record.pk]),
            'decision': record.decision_display, 'status': record.status}


@never_cache
@login_required
@require_POST
@coordinator_required
def recruitment_notified(request, pk):
    value = request.POST.get('notified')
    if value not in ('yes', 'no'):
        return JsonResponse({'error': 'Wybierz Tak lub Nie.'}, status=400)
    with transaction.atomic():
        record = get_object_or_404(Recruitment.objects.select_for_update(), pk=pk)
        try:
            save_notification(record, request.user, value == 'yes', request.POST.get('version', ''))
        except NotificationConflict as error:
            return JsonResponse({'error': str(error), **notification_response(record, request.user)}, status=409)
    return JsonResponse(notification_response(record, request.user))
