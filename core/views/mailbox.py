from zipfile import BadZipFile
from copy import deepcopy
import re
from lxml.etree import XMLSyntaxError
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from core.mailbox_date import MailboxDateForm
from core.models import MailboxConnection, MailboxDownload
from core.permissions import superuser_required
from core.services.mailbox import read_headers, MailboxError
from core.services.mailbox_import import (
    default_mailbox, mailbox_key, receipts, fetch_messages, prepare_forms, package_messages, describe_import_error, filter_valid_messages, MAX_MESSAGES,
)
from core.services.document_converter import ConversionError, RebuildConfirmationRequired, REBUILD_WARNING
from core.services.reviews import import_reviews

HEADERS_SESSION_KEY = 'mailbox_headers_snapshot'
IMPORT_OPTIONS = ('show_downloaded', 'clean', 'rebuild', 'convert', 'subject_filter')


def restore_headers(request, config, key, context):
    """The private session stores headers only, never bodies or attachments."""
    saved = request.session.get(HEADERS_SESSION_KEY)
    if not saved:
        return
    if 'sent_since' not in saved or saved.get('mailbox') != key or saved.get('user') != request.user.pk:
        request.session.pop(HEADERS_SESSION_KEY, None)
        return
    result = deepcopy(saved['result'])
    already = set(receipts(config, result['validity']).values_list('uid', flat=True))
    for row in result['rows']:
        row['downloaded'] = row['uid'] in already
    base = {'user': request.user.pk, 'mailbox': key, 'subject_filter': saved['subject_filter'], 'sent_since': saved['sent_since']}
    for field in ('next_cursor', 'previous_cursor'):
        if result.get(field):
            result[field] = signing.dumps(dict(base, cursor=result[field],
                show_downloaded=saved['show_downloaded']), salt='mailbox-cursor', compress=True)
    context['result'] = result
    # Renew signatures from trusted session data without refetching the mailbox.
    context['selection'] = signing.dumps(dict(base, validity=result['validity'],
        uids=[row['uid'] for row in result['rows']], show_downloaded=saved['show_downloaded']), salt='mailbox-selection', compress=True)
    if request.method == 'GET':
        context.update({name: saved[name] for name in IMPORT_OPTIONS})
        context['date_form'] = MailboxDateForm(initial=saved['date_filter'])


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
    archive = None
    date_form = MailboxDateForm(request.POST if request.method == 'POST' else None)
    context = {'date_form': date_form, 'result': None, 'max_messages': MAX_MESSAGES, 'show_downloaded': request.POST.get('show_downloaded') == 'on',
               'clean': request.POST.get('clean') == 'on' if request.method == 'POST' else True,
               'rebuild': request.POST.get('rebuild') == 'on' if request.method == 'POST' else True,
               'convert': request.POST.get('convert') == 'on' if request.method == 'POST' else True}
    try:
        config = default_mailbox()
        context['mailbox'] = config
        context['subject_choices'] = config.subject_choices()
        key = mailbox_key(config)
        restore_headers(request, config, key, context)
        subject_filter = request.POST.get('subject_filter', '') if request.method == 'POST' else context.get('subject_filter', '')
        context['subject_filter'] = subject_filter
        if subject_filter and subject_filter not in context['subject_choices']:
            raise MailboxError('Wybrany nabór nie jest już dostępny. Wybierz go ponownie.')
        base = {'user': request.user.pk, 'mailbox': key}
        if request.method == 'POST':
            if not date_form.is_valid():
                raise MailboxError('Popraw datę początkową.')
            sent_since = date_form.effective_date()
            base['sent_since'] = sent_since
            action = request.POST.get('action', 'headers')
            if action in ('download', 'confirm'):
                selection = load_token(request.POST.get('selection', ''), request.user, key, 'mailbox-selection')
                if selection.get('subject_filter', '') != subject_filter or selection.get('sent_since', '') != sent_since:
                    raise MailboxError('Zmieniono nabór lub datę. Najpierw pobierz nagłówki ponownie.')
                try:
                    selected = sorted(set(int(value) for value in request.POST.getlist('uids')))
                except ValueError:
                    raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
                if not selected or not set(selected).issubset(selection['uids']):
                    raise MailboxError('Wybierz wiadomości z pobranej listy.')
                validity = selection['validity']
                skipped_errors = []
                messages = fetch_messages(config, validity, selected, skipped_errors)
                digest = [[m['uid'], m['digest']] for m in messages]
                confirmation = None
                if action == 'confirm':
                    confirmation = load_token(request.POST.get('preview', ''), request.user, key, 'mailbox-preview')
                    if confirmation['digest'] != digest or confirmation['clean'] != context['clean'] or confirmation['convert'] != context['convert'] or confirmation.get('rebuild') != context['rebuild']:
                        confirmation = None
                        context['error'] = 'Wiadomości lub opcje się zmieniły. Sprawdź nowy podgląd i zatwierdź go ponownie.'
                tokens = confirmation['warnings'] if confirmation else {}
                approved = bool(confirmation and request.POST.get('approve') == 'on')
                messages = filter_valid_messages(messages, config, validity, request.user, skipped_errors)
                accepted, rebuild_warnings = [], []
                archive = package_messages(messages, clean=context['clean'], convert=context['convert'],
                    rebuild=context['rebuild'], allow_rebuild_omissions=request.POST.get('allow_rebuild_omissions') == 'on',
                    skipped_errors=skipped_errors, accepted=accepted, rebuild_warnings=rebuild_warnings)
                messages = accepted
                selected = [message['uid'] for message in messages]
                digest = [[m['uid'], m['digest']] for m in messages]
                if confirmation and confirmation['digest'] != digest:
                    approved = False
                    tokens = {}
                    context['error'] = 'Pominięto wiadomości z błędami. Sprawdź nowy podgląd i zatwierdź pozostałe zgłoszenia.'
                forms = prepare_forms(messages, config, validity, request.user, tokens, approved)
                hard_errors = [describe_import_error(e, form.mail_sources) for form in forms for field, errors in form.errors.items()
                               if field != 'confirm_submission_warnings' for e in errors]
                senders = {row['uid']: row.get('sender') or 'Nieznana osoba' for row in (context.get('result') or {}).get('rows', [])}
                skipped_errors = [re.sub(r'\bWiadomość\s+(\d+)', lambda match: senders.get(int(match[1]), 'Zgłoszenie'), error)
                                  for error in skipped_errors]
                context.update(preview_messages=messages, forms=forms, selection=request.POST['selection'],
                               selected=selected, hard_errors=hard_errors, skipped_errors=skipped_errors)
                if rebuild_warnings:
                    context['rebuild_warning'] = True
                    context['rebuild_warning_text'] = ' '.join(rebuild_warnings)
                if not messages:
                    context['error'] = 'Brak poprawnych zgłoszeń do zaimportowania. Niczego nie zapisano.'
                warnings = {str(form.data['anthology']): form.data.get('submission_warnings_token', '') for form in forms}
                context['preview_token'] = signing.dumps(dict(base, digest=digest, warnings=warnings,
                    clean=context['clean'], convert=context['convert'], rebuild=context['rebuild']), salt='mailbox-preview', compress=True)
                if action == 'confirm' and approved and messages and not rebuild_warnings and all(form.is_valid() for form in forms):
                    # Files have been prepared before database writes or locks.
                    with transaction.atomic():
                        locked = MailboxConnection.objects.select_for_update().get(pk=config.pk)
                        if not locked.is_active or locked.purpose != MailboxConnection.Purpose.SUBMISSIONS or mailbox_key(locked) != key:
                            raise MailboxError('Ustawienia skrzynki się zmieniły. Pobierz nagłówki ponownie.')
                        # Receipt uniqueness and this lock prevent duplicate imports
                        # on retries, double-clicks and concurrent downloads.
                        current_forms = prepare_forms(messages, config, validity, request.user, tokens, True)
                        for form in current_forms:
                            try:
                                import_reviews(user=request.user, form=form)
                            except ValidationError as error:
                                raise MailboxError(' '.join(describe_import_error(detail, form.mail_sources) for detail in error.messages)) from None
                        for message in messages:
                            MailboxDownload.objects.update_or_create(mailbox_key=key,
                                uid_validity=validity, uid=message['uid'], defaults={'fingerprint': message.get('fingerprint', ''), 'message_id': message.get('message_id', '')})
                    response = FileResponse(archive, as_attachment=True, filename='zgloszenia.zip', content_type='application/zip')
                    archive = None  # The streaming response owns and closes this file.
                    response['Cache-Control'] = 'private, no-store'
                    response['X-Content-Type-Options'] = 'nosniff'
                    return response
                if action == 'confirm' and not approved and not context.get('error'):
                    context['error'] = 'Sprawdź podgląd i zaznacz potwierdzenie.'
            elif action == 'copy_emails':
                from core.services.mailbox_email_copy import sender_emails
                selection = load_token(request.POST.get('selection', ''), request.user, key, 'mailbox-selection')
                if selection.get('sent_since', '') != sent_since or selection.get('subject_filter', '') != subject_filter or selection.get('show_downloaded', False) != context['show_downloaded']:
                    raise MailboxError('Zmieniono filtry. Pobierz nagłówki ponownie.')
                return JsonResponse({'emails': sender_emails(config, selection['validity'],
                    excluded=None if context['show_downloaded'] else lambda validity: receipts(config, validity).values_list('uid', flat=True),
                    subject_filter=subject_filter, sent_since=sent_since)})
            elif action == 'headers':
                cursor = None
                if request.POST.get('cursor'):
                    page = load_token(request.POST['cursor'], request.user, key, 'mailbox-cursor')
                    if page.get('sent_since', '') == sent_since and page.get('subject_filter', '') == subject_filter and page.get('show_downloaded', False) == context['show_downloaded']:
                        cursor = page['cursor']
                excluded = None if context['show_downloaded'] else lambda validity: receipts(config, validity).values_list('uid', flat=True)
                result = read_headers(config, cursor, excluded=excluded, subject_filter=subject_filter, sent_since=sent_since)
                request.session[HEADERS_SESSION_KEY] = dict(base, result=result, date_filter=date_form.snapshot(),
                    **{name: context[name] for name in IMPORT_OPTIONS})
                # Refresh repeats a GET, not the IMAP fetch submitted by POST.
                return redirect(request.path)
            else:
                raise MailboxError('Nieprawidłowa operacja.')
    except RebuildConfirmationRequired as error:
        context['rebuild_warning'] = True
        context['rebuild_warning_text'] = str(error)
        context['error'] = str(error)
    except (MailboxError, ConversionError) as error:
        senders = {row['uid']: row.get('sender') or 'Nieznana osoba' for row in (context.get('result') or {}).get('rows', [])}
        context['error'] = re.sub(r'\bWiadomość\s+(\d+)', lambda match: senders.get(int(match[1]), 'Zgłoszenie'), str(error))
    except (BadZipFile, XMLSyntaxError, ValueError, OSError):
        context['error'] = 'Nie udało się przetworzyć pliku. Sprawdź załączniki lub spróbuj pobrać same oryginały.'
    except ValidationError as error:
        context['error'] = ' '.join(error.messages)
    finally:
        if archive is not None:
            archive.close()
    return render(request, 'core/review_bulk_import.html', context,
                  status=400 if context.get('error') else 200)
