from zipfile import BadZipFile
from lxml.etree import XMLSyntaxError
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from core.models import MailboxConnection, MailboxDownload
from core.permissions import superuser_required
from core.services.mailbox import read_headers, MailboxError
from core.services.mailbox_import import (
    default_mailbox, mailbox_key, receipts, fetch_messages, prepare_forms, package_messages,
)
from core.services.document_converter import ConversionError, RebuildConfirmationRequired, REBUILD_WARNING
from core.services.reviews import import_reviews


def load_token(token, user, key, salt):
    try:
        payload = signing.loads(token, salt=salt, max_age=3600)
        if payload['user'] != user.pk or payload['mailbox'] != key:
            raise ValueError()
        return payload
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise MailboxError('Podgląd wygasł lub ustawienia skrzynki się zmieniły. Pobierz nagłówki ponownie.') from None


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
@sensitive_post_parameters()
def mailbox_headers(request):
    context = {'result': None, 'show_downloaded': request.POST.get('show_downloaded') == 'on',
               'clean': request.POST.get('clean') == 'on' if request.method == 'POST' else True,
               'rebuild': request.POST.get('rebuild') == 'on' if request.method == 'POST' else True,
               'convert': request.POST.get('convert') == 'on' if request.method == 'POST' else True}
    try:
        config = default_mailbox()
        context['mailbox'] = config
        context['subject_choices'] = config.subject_choices()
        subject_filter = request.POST.get('subject_filter', '')
        context['subject_filter'] = subject_filter
        if subject_filter and subject_filter not in context['subject_choices']:
            raise MailboxError('Wybrany nabór nie jest już dostępny. Wybierz go ponownie.')
        key = mailbox_key(config)
        base = {'user': request.user.pk, 'mailbox': key}
        if request.method == 'POST':
            action = request.POST.get('action', 'headers')
            if action in ('download', 'confirm'):
                selection = load_token(request.POST.get('selection', ''), request.user, key, 'mailbox-selection')
                if selection.get('subject_filter', '') != subject_filter:
                    raise MailboxError('Zmieniono nabór. Najpierw pobierz nagłówki ponownie.')
                try:
                    selected = sorted(set(int(value) for value in request.POST.getlist('uids')))
                except ValueError:
                    raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
                if not selected or not set(selected).issubset(selection['uids']):
                    raise MailboxError('Wybierz wiadomości z pobranej listy.')
                validity = selection['validity']
                messages = fetch_messages(config, validity, selected)
                digest = [[m['uid'], m['digest']] for m in messages]
                confirmation = None
                if action == 'confirm':
                    confirmation = load_token(request.POST.get('preview', ''), request.user, key, 'mailbox-preview')
                    if confirmation['digest'] != digest or confirmation['clean'] != context['clean'] or confirmation['convert'] != context['convert'] or confirmation.get('rebuild') != context['rebuild']:
                        raise MailboxError('Wiadomości lub opcje się zmieniły. Przygotuj podgląd ponownie.')
                tokens = confirmation['warnings'] if confirmation else {}
                approved = bool(confirmation and request.POST.get('approve') == 'on')
                forms = prepare_forms(messages, config, validity, request.user, tokens, approved)
                hard_errors = [str(e) for form in forms for field, errors in form.errors.items()
                               if field != 'confirm_submission_warnings' for e in errors]
                context.update(preview_messages=messages, forms=forms, selection=request.POST['selection'],
                               selected=selected, hard_errors=hard_errors)
                warnings = {str(form.data['anthology']): form.data.get('submission_warnings_token', '') for form in forms}
                context['preview_token'] = signing.dumps(dict(base, digest=digest, warnings=warnings,
                    clean=context['clean'], convert=context['convert'], rebuild=context['rebuild']), salt='mailbox-preview', compress=True)
                if action == 'confirm' and approved and all(form.is_valid() for form in forms):
                    # Expensive operations finish before any database writes or locks.
                    archive = package_messages(messages, clean=context['clean'], convert=context['convert'], rebuild=context['rebuild'], allow_rebuild_omissions=request.POST.get('allow_rebuild_omissions') == 'on')
                    try:
                        with transaction.atomic():
                            locked = MailboxConnection.objects.select_for_update().get(pk=config.pk)
                            if not locked.is_active or mailbox_key(locked) != key:
                                raise MailboxError('Ustawienia skrzynki się zmieniły. Pobierz nagłówki ponownie.')
                            # Receipt uniqueness and this lock prevent duplicate imports
                            # on retries, double-clicks and concurrent downloads.
                            current_forms = prepare_forms(messages, config, validity, request.user, tokens, True)
                            for form in current_forms:
                                import_reviews(user=request.user, form=form)
                            for message in messages:
                                MailboxDownload.objects.update_or_create(mailbox_key=key,
                                    uid_validity=validity, uid=message['uid'], defaults={'fingerprint': message.get('fingerprint', ''), 'message_id': message.get('message_id', '')})
                        response = FileResponse(archive, as_attachment=True, filename='zgloszenia.zip', content_type='application/zip')
                        response['Cache-Control'] = 'private, no-store'
                        response['X-Content-Type-Options'] = 'nosniff'
                        return response
                    except BaseException:
                        archive.close()
                        raise
                if action == 'confirm' and not approved:
                    context['error'] = 'Sprawdź podgląd i zaznacz potwierdzenie.'
            elif action == 'headers':
                cursor = None
                if request.POST.get('cursor'):
                    page = load_token(request.POST['cursor'], request.user, key, 'mailbox-cursor')
                    if page.get('subject_filter', '') == subject_filter and page.get('show_downloaded', False) == context['show_downloaded']:
                        cursor = page['cursor']
                excluded = None if context['show_downloaded'] else lambda validity: receipts(config, validity).values_list('uid', flat=True)
                result = read_headers(config, cursor, excluded=excluded, subject_filter=subject_filter)
                already = set(receipts(config, result['validity']).values_list('uid', flat=True))
                for row in result['rows']:
                    row['downloaded'] = row['uid'] in already
                for field in ('next_cursor', 'previous_cursor'):
                    if result.get(field):
                        result[field] = signing.dumps(dict(base, cursor=result[field], subject_filter=subject_filter, show_downloaded=context['show_downloaded']), salt='mailbox-cursor', compress=True)
                context['result'] = result
                context['selection'] = signing.dumps(dict(base, subject_filter=subject_filter, validity=result['validity'],
                    uids=[row['uid'] for row in result['rows']]), salt='mailbox-selection', compress=True)
            else:
                raise MailboxError('Nieprawidłowa operacja.')
    except RebuildConfirmationRequired as error:
        context['rebuild_warning'] = True
        context['rebuild_warning_text'] = str(error)
        context['error'] = str(error)
    except (MailboxError, ConversionError) as error:
        context['error'] = str(error)
    except (BadZipFile, XMLSyntaxError, ValueError, OSError):
        context['error'] = 'Nie udało się przetworzyć pliku. Sprawdź załączniki lub spróbuj pobrać same oryginały.'
    except ValidationError as error:
        context['error'] = ' '.join(error.messages)
    return render(request, 'core/review_bulk_import.html', context,
                  status=400 if context.get('error') else 200)
