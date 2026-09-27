import hashlib
import json
from django.core import signing
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from core.models import MailboxConnection
from core.permissions import superuser_required
from core.services.mailbox import read_headers, MailboxError


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
@sensitive_post_parameters()
def mailbox_headers(request):
    mailboxes = MailboxConnection.objects.filter(is_active=True)
    context = {'mailboxes': mailboxes, 'selected_mailbox': None, 'result': None}
    if request.method == 'POST':
        raw = request.POST.get('mailbox', '')
        config = mailboxes.filter(pk=int(raw)).first() if raw.isascii() and raw.isdecimal() and len(raw) < 19 else None
        if config is None:
            context['error'] = 'Wybierz aktywną skrzynkę.'
        else:
            context['selected_mailbox'] = config.pk
            fingerprint = hashlib.sha256(json.dumps([
                config.pk, config.host, config.port, config.security, config.username, config.folder,
            ]).encode()).hexdigest()
            token = request.POST.get('cursor', '')
            try:
                cursor = None
                if token:
                    try:
                        payload = signing.loads(token, salt='mailbox-cursor', max_age=3600)
                    except signing.BadSignature:
                        raise MailboxError('Podgląd wygasł lub link jest nieprawidłowy. Pobierz nagłówki od początku.') from None
                    if payload.get('user') != request.user.pk or payload.get('mailbox') != fingerprint:
                        raise MailboxError('Ustawienia skrzynki zmieniły się. Pobierz nagłówki od początku.')
                    cursor = payload['cursor']
                result = read_headers(config, cursor)
                for field in ('next_cursor', 'previous_cursor'):
                    if result.get(field):
                        result[field] = signing.dumps({'user': request.user.pk, 'mailbox': fingerprint,
                            'cursor': result[field]}, salt='mailbox-cursor', compress=True)
                context['result'] = result
            except MailboxError as error:
                context['error'] = str(error)
    return render(request, 'core/review_bulk_import.html', context,
        status=400 if context.get('error') else 200)
