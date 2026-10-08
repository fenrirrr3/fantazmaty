"""Read-only recruitment mailbox. Structured candidate mapping is configured later."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.models import MailboxConnection, MailboxDownload
from core.permissions import superuser_required
from core.recruitment_roles import ROLE_CHOICES
from core.services.mailbox import MailboxError, read_headers
from core.services.mailbox_import import fetch_messages, mailbox_key, receipts, MAX_MESSAGES

SALT = 'recruitment-mailbox'


class RecruitmentMailboxForm(forms.Form):
    roles = forms.MultipleChoiceField(label='Rola', choices=ROLE_CHOICES,
        help_text='Wybierz jedną lub kilka ról. Temat może zawierać kilka ról; wystarczy dopasowanie jednej z wybranych.')
    show_downloaded = forms.BooleanField(label='Pokaż również już pobrane wiadomości', required=False)


def recruitment_connection():
    boxes = list(MailboxConnection.objects.filter(
        purpose=MailboxConnection.Purpose.RECRUITMENT, is_active=True)[:2])
    if len(boxes) != 1:
        raise MailboxError('Ustaw w panelu administratora jedną aktywną skrzynkę z przeznaczeniem „Rekrutacja do zespołu”.')
    return boxes[0]


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def recruitment_mailbox(request):
    form = RecruitmentMailboxForm(request.POST if request.method == 'POST' else None)
    context = {'form': form}
    try:
        config = recruitment_connection()
        context['mailbox'] = config
        if request.method == 'POST' and form.is_valid():
            roles = [key for key, _ in ROLE_CHOICES if key in form.cleaned_data['roles']]
            show = form.cleaned_data['show_downloaded']
            identity = {'user': request.user.pk, 'mailbox': mailbox_key(config), 'roles': roles, 'show': show}

            def signed(payload):
                return signing.dumps({**identity, **payload}, salt=SALT, compress=True)

            def load(value, kind):
                try:
                    payload = signing.loads(value, salt=SALT, max_age=3600)
                except signing.BadSignature:
                    raise MailboxError('Wybór wygasł. Pobierz nagłówki ponownie.') from None
                if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in identity.items()) or payload.get('kind') != kind:
                    raise MailboxError('Zmieniono filtry lub skrzynkę. Pobierz nagłówki od początku.')
                return payload

            action = request.POST.get('action', 'headers')
            if action == 'copy_emails':
                from core.services.mailbox_email_copy import sender_emails
                selection = load(request.POST.get('selection', ''), 'selection')
                return JsonResponse({'emails': sender_emails(config, selection['validity'],
                    excluded=None if show else lambda validity: receipts(config, validity).values_list('uid', flat=True),
                    recruitment_roles=roles)})
            if action == 'download':
                selection = load(request.POST.get('selection', ''), 'selection')
                try:
                    selected = {int(value) for value in request.POST.getlist('selected')}
                except (ValueError, TypeError):
                    raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
                if not 1 <= len(selected) <= MAX_MESSAGES or not selected.issubset(selection['uids']):
                    raise MailboxError(f'Wybierz od 1 do {MAX_MESSAGES} wiadomości z pobranej listy.')
                # No submission parser: a recruitment message need not contain an
                # attachment or author/title fields. Preserve its exact MIME source.
                messages = fetch_messages(config, selection['validity'], selected, raw_messages=True)
                stream = BytesIO()
                with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
                    for message in messages:
                        archive.writestr(f'rekrutacja-{message["uid"]}.eml', message['raw'])
                with transaction.atomic():
                    current = MailboxConnection.objects.select_for_update().get(pk=config.pk)
                    if not current.is_active or current.purpose != MailboxConnection.Purpose.RECRUITMENT or mailbox_key(current) != identity['mailbox']:
                        raise MailboxError('Ustawienia skrzynki zmieniły się. Pobierz nagłówki ponownie.')
                    for message in messages:
                        MailboxDownload.objects.update_or_create(mailbox_key=identity['mailbox'],
                            uid_validity=selection['validity'], uid=message['uid'], defaults={})
                stream.seek(0)
                return FileResponse(stream, as_attachment=True, filename='rekrutacja-wiadomosci.zip')
            if action != 'headers':
                raise MailboxError('Nieznana operacja.')
            cursor = load(request.POST['cursor'], 'cursor')['cursor'] if request.POST.get('cursor') else None
            result = read_headers(config, cursor,
                excluded=None if show else lambda validity: receipts(config, validity).values_list('uid', flat=True),
                recruitment_roles=roles)
            downloaded = set(receipts(config, result['validity']).values_list('uid', flat=True))
            for row in result['rows']:
                row['downloaded'] = row['uid'] in downloaded
            context.update(result)
            context['selection'] = signed({'kind': 'selection', 'validity': result['validity'], 'uids': [row['uid'] for row in result['rows']]})
            for name in ('next_cursor', 'previous_cursor'):
                context[name] = signed({'kind': 'cursor', 'cursor': result[name]}) if result[name] else ''
        elif request.method == 'POST':
            return render(request, 'core/recruitment_mailbox.html', context, status=400)
    except MailboxError as error:
        context['error'] = str(error)
        return render(request, 'core/recruitment_mailbox.html', context, status=400 if request.method == 'POST' else 200)
    return render(request, 'core/recruitment_mailbox.html', context)
